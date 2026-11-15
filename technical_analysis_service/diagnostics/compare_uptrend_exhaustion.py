"""Read-only historical comparison of the breakaway-veto hypothesis."""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .strategy_miss_audit import (_stored_and_calendar, calendar_alignment,
                                 classify_outcomes, daily_price_context, _excel_ready)


def compare(start: date, end: date, destination: Path):
    from technical_analysis_service.analysis.cascade.dataset import build_base
    from technical_analysis_service.analysis.cascade.strategies import nifty_2026_call_put_research
    from technical_analysis_service.common.config import get_underlying_lookback_days, get_nifty_target_pct
    from technical_analysis_service.runtime import SERVICE_ROOT

    base = build_base().sort_values('signal_date').reset_index(drop=True)
    base['signal_date'] = pd.to_datetime(base['signal_date']).dt.strftime('%Y-%m-%d')
    base = daily_price_context(base)
    _, calendar = _stored_and_calendar(start, end)
    trading = pd.to_datetime(calendar.loc[calendar.is_trading_day, 'calendar_date']).dt.strftime('%Y-%m-%d').tolist()
    base['calendar_alignment_valid'] = calendar_alignment(base, trading, get_underlying_lookback_days())
    base['scoring_label'] = base.actual_trade_label.where(base.calendar_alignment_valid)
    # Frozen pre-veto rule, retained explicitly for reproducible comparisons.
    old = ((base.close_1515 >= base.ma20 * 1.025) & (base.ma20_slope > 0)
           & ((base.price_zscore_20d > 1.5) | (base.rsi5 > 75))
           & (base.pcr_oi <= 1.15) & (base.nifty_gap_pct >= 0))
    efficiency = pd.to_numeric(base.get('trend_efficiency_5d', pd.Series(np.nan, index=base.index)), errors='coerce')
    gap = pd.to_numeric(base.gap_open_atr, errors='coerce')
    base['efficiency_veto'] = efficiency > 0.9
    base['up_gap_veto'] = gap > 1.0
    base['veto_input_missing'] = efficiency.isna() | gap.isna()
    base['baseline_prediction'] = np.where(old, 'PUT', 'NO_POSITION')
    current = nifty_2026_call_put_research(base)['strategy_UptrendExhaustionPut_signal']
    assert (current == base.baseline_prediction).all(), 'Production rule differs from the frozen baseline'
    # Rejected candidate stays confined to this offline diagnostic.
    base['revised_prediction'] = np.where(old & ~(base.efficiency_veto | base.up_gap_veto), 'PUT', 'NO_POSITION')
    base = base[base.signal_date.between(str(start), str(end))].copy()
    base['year'] = base.signal_date.str[:4]
    base['changed'] = base.baseline_prediction != base.revised_prediction
    summaries = []
    source_summaries = []
    for period, rows in [('ALL', base), *list(base.groupby('year'))]:
        for version in ['baseline', 'revised']:
            outcome = classify_outcomes(rows.scoring_label, rows[version+'_prediction'], 'PUT')
            fires = int((outcome.graded & outcome.fired).sum())
            correct = int(outcome.correct_fire.sum())
            opportunities = int(outcome.opportunity.sum())
            summaries.append(dict(period=period, version=version, source_sessions=len(rows),
                graded_sessions=int(outcome.graded.sum()), all_fires=int(outcome.fired.sum()),
                graded_fires=fires, correct_fires=correct,
                precision_misses=int(outcome.precision_miss.sum()), recall_misses=int(outcome.recall_miss.sum()),
                opportunities=opportunities, precision=correct/fires if fires else np.nan,
                recall=correct/opportunities if opportunities else np.nan))
            source_outcome = classify_outcomes(rows.actual_trade_label, rows[version+'_prediction'], 'PUT')
            source_fires = int((source_outcome.graded & source_outcome.fired).sum())
            source_correct = int(source_outcome.correct_fire.sum())
            source_opportunities = int(source_outcome.opportunity.sum())
            source_summaries.append(dict(period=period, version=version, source_sessions=len(rows),
                graded_sessions=int(source_outcome.graded.sum()), all_fires=int(source_outcome.fired.sum()),
                graded_fires=source_fires, correct_fires=source_correct,
                precision_misses=int(source_outcome.precision_miss.sum()),
                recall_misses=int(source_outcome.recall_miss.sum()), opportunities=source_opportunities,
                precision=source_correct/source_fires if source_fires else np.nan,
                recall=source_correct/source_opportunities if source_opportunities else np.nan))
            for column in outcome:
                base.loc[rows.index, version+'_calendar_verified_'+column] = outcome[column]
                base.loc[rows.index, version+'_source_'+column] = source_outcome[column]
    fields = ['signal_date','next_trade_date','year','actual_trade_label','scoring_label',
              'calendar_alignment_valid','baseline_prediction','revised_prediction','changed',
              'trend_efficiency_5d','gap_open_atr','nifty_gap_pct','efficiency_veto','up_gap_veto',
              'veto_input_missing','rsi_divergence_flag','close_1515','ma20','ma20_slope',
              'rsi5','price_zscore_20d','pcr_oi','next_open','next_close',
              'execution_open_to_close_pct','execution_max_up_pct','execution_max_down_pct']
    fields += [c for c in base if c.startswith(('baseline_', 'revised_')) and c not in fields]
    daily = base.reindex(columns=fields)
    calendar['calendar_date'] = pd.to_datetime(calendar.calendar_date).dt.strftime('%Y-%m-%d')
    coverage = calendar[calendar.calendar_date.between(str(start), str(end))].copy()
    coverage = coverage.set_index('calendar_date').reindex(pd.date_range(start,end).strftime('%Y-%m-%d')).rename_axis('calendar_date').reset_index()
    coverage['calendar_record_available'] = coverage.is_trading_day.notna()
    coverage['features_available'] = coverage.calendar_date.isin(base.signal_date)
    coverage['source_calendar_disagreement'] = coverage.features_available & coverage.is_trading_day.eq(False)
    notes = [
        ('Decision', 'Rejected by user: original production rule retained. Revised predictions are a hypothetical offline candidate only.'),
        ('Scope', f'{start} through {end}; current-code offline replay, not historical live decisions.'),
        ('Change', 'Original rule plus veto: efficiency_5d > 0.9 OR upward gap_open_atr > 1.0. Strict inequalities; missing inputs do not veto.'),
        ('Timing', 'signal_date uses completed-day features and next execution-session opening gap, as in the existing rule; not a pre-open forecast.'),
        ('Labels', f'Next {get_underlying_lookback_days()} session(s), target {get_nifty_target_pct():.2%} from next_open. PUT/BOTH count as PUT opportunities; BOTH does not establish first touch or profitable execution.'),
        ('Scoring', 'Precision = correct / graded fires; recall = correct / all graded PUT-or-BOTH opportunities. Summary uses original source-row labels; Calendar_verified_summary additionally excludes calendar-misaligned/unverifiable labels identically in both versions.'),
        ('Calendar limitation', 'No 2025 calendar records exist locally. All 2025 source-label results are unverified against an exchange calendar; they are retained in Summary and excluded from Calendar_verified_summary. Known feature gaps also affect source-row labels. Coverage separates absent calendar records from explicit closed days.'),
        ('Missing data', 'Missing feature sessions appear in Coverage and are not treated as no-fire decisions. No data repaired or database records changed.'),
        ('Hypothesis', 'Chosen after inspecting two 2026 examples. Historical comparison is in-sample evidence, not independent validation. No RSI-divergence gate added.'),
        ('Interpretation', 'A veto cannot increase raw-strategy recall on a fixed cohort: it removes signals. Precision may improve while recall stays equal or falls.'),
        ('Scope of comparison', 'Standalone UptrendExhaustionPut votes, not full-cascade outcomes, option P&L or refreshed dashboard backtests.'),
    ]
    frames = {'Readme': pd.DataFrame(notes, columns=['item','meaning']),
              'Summary': pd.DataFrame(source_summaries),
              'Calendar_verified_summary': pd.DataFrame(summaries), 'Changed_signals': daily[daily.changed],
              'All_baseline_fires': daily[daily.baseline_prediction.eq('PUT')],
              'Daily_comparison': daily, 'Coverage': coverage}
    destination.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(destination/'UptrendExhaustionPut_comparison_2024_to_date.xlsx', engine='openpyxl') as writer:
        for name, frame in frames.items():
            frame.to_csv(destination/(name.lower()+'.csv'), index=False)
            _excel_ready(frame).to_excel(writer, sheet_name=name, index=False)
            sheet = writer.sheets[name]; sheet.freeze_panes='A2'; sheet.auto_filter.ref=sheet.dimensions
            for cells in sheet.iter_cols(min_row=1, max_row=1):
                sheet.column_dimensions[cells[0].column_letter].width = min(55, max(17,len(str(cells[0].value))+2))
                if cells[0].value in ('precision','recall'):
                    for cell in sheet[cells[0].column_letter][1:]: cell.number_format='0.00%'
        writer.sheets['Readme'].column_dimensions['B'].width=115
    metadata = dict(start=str(start), end=str(end), source_sessions=len(base),
        changed_signals=int(base.changed.sum()), missing_feature_dates=coverage.loc[coverage.is_trading_day.eq(True) & ~coverage.features_available,'calendar_date'].tolist(),
        missing_calendar_dates=coverage.loc[~coverage.calendar_record_available,'calendar_date'].tolist(),
        excluded_label_dates=base.loc[base.actual_trade_label.notna() & ~base.calendar_alignment_valid,'signal_date'].tolist(),
        source_dates_outside_calendar=sorted(set(base.signal_date)-set(trading)),
        rule_sha256=hashlib.sha256((SERVICE_ROOT/'analysis/cascade/strategies.py').read_bytes()).hexdigest())
    (destination/'metadata.json').write_text(json.dumps(metadata, indent=2)+'\n', encoding='utf-8')
    print(frames['Summary'].to_string(index=False), flush=True)
    print(json.dumps({key: len(value) if isinstance(value,list) else value for key,value in metadata.items()}, indent=2), flush=True)
    return frames


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', type=date.fromisoformat, default=date(2024,1,1))
    parser.add_argument('--end', type=date.fromisoformat, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    if args.start > args.end: parser.error('start must not exceed end')
    compare(args.start, args.end, args.output_dir)
