"""Read-only research backfill, label metrics, conflicts and intraday trade replay."""
from __future__ import annotations

import argparse
from datetime import date, time
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from technical_analysis_service.analysis.cascade.streak_continuation import (
    NAME, TRADE_POLICY, streak_continuation_details, research_conflict_signals,
    add_streak_history_context)
from .strategy_miss_audit import (_stored_and_calendar, calendar_alignment,
                                 classify_outcomes, _excel_ready)


def simulate_long_trade(candles: pd.DataFrame, trade_date, entry: float) -> dict:
    """5m OHLC replay. Ambiguous same-bar touches have return bounds, not a win.

    Bar timestamps denote start times; the 15:10 bar's OPEN is the time exit.
    Coverage is required from 09:15 until exit. No fill is invented across gaps.
    """
    if not np.isfinite(entry) or entry <= 0:
        return dict(execution_status='MISSING_ENTRY')
    day = pd.Timestamp(trade_date).normalize()
    exit_at = day + pd.Timedelta(hours=15,minutes=10)
    expected = day + pd.Timedelta(hours=9,minutes=15)
    target, stop = entry*1.006, entry*.996
    if candles.empty: return dict(execution_status='MISSING_INTRADAY')
    bars = candles.copy()
    bars['candle_time'] = pd.to_datetime(bars.candle_time)
    if bars.candle_time.duplicated().any(): return dict(execution_status='DUPLICATE_BARS')
    bars = bars.set_index('candle_time')
    def result(status, at, lower, upper=None):
        return dict(execution_status=status, exit_time_ist=str(at),
            underlying_return_lower=lower, underlying_return_upper=lower if upper is None else upper,
            sized_return_lower=lower*.5, sized_return_upper=(lower if upper is None else upper)*.5)
    while expected <= exit_at:
        if expected not in bars.index:
            return dict(execution_status='INCOMPLETE_INTRADAY', missing_bar_ist=str(expected))
        row=bars.loc[expected]
        prices=pd.to_numeric(row[['open_price','high_price','low_price','close_price']], errors='coerce')
        if not np.isfinite(prices).all(): return dict(execution_status='INVALID_BAR')
        opening, high, low, closing = prices.tolist()
        if low > min(opening,closing) or high < max(opening,closing) or low <= 0:
            return dict(execution_status='INVALID_BAR')
        if expected.time()==time(9,15) and not np.isclose(opening,entry,rtol=0,atol=.1):
            return dict(execution_status='ENTRY_SOURCE_MISMATCH', candle_entry=opening, feature_entry=entry)
        if opening <= stop: return result('STOP_GAP',expected,opening/entry-1)
        if opening >= target: return result('TARGET',expected,.006)
        if expected == exit_at: return result('TIME_EXIT',expected,opening/entry-1)
        if low <= stop and high >= target:
            return result('AMBIGUOUS_BOTH_IN_BAR',expected,-.004,.006)
        if low <= stop: return result('STOP',expected,-.004)
        if high >= target: return result('TARGET',expected,.006)
        expected += pd.Timedelta(minutes=5)
    raise AssertionError('unreachable')


def load_candles(start, end):
    from technical_analysis_service.common.config import get_settings
    from technical_analysis_service.data_manager.db.client_factory import get_database_client
    db=get_database_client(get_settings()); db.connect()
    try:
        with db.conn.cursor() as cur:
            cur.execute('SET TRANSACTION READ ONLY')
            cur.execute('SELECT trade_date,candle_time,open_price,high_price,low_price,close_price FROM "UnderlyingCandle5m" WHERE underlying=%s AND trade_date BETWEEN %s AND %s ORDER BY candle_time',('NIFTY',start,end))
            return pd.DataFrame(cur.fetchall(), columns=[c[0] for c in cur.description])
    finally: db.close()


def summary_rows(daily):
    result=[]
    for cohort in ['SOURCE_LABEL','CALENDAR_VERIFIED']:
        for period, rows in [('ALL',daily), *list(daily.groupby('year'))]:
            labels=rows.actual_trade_label.where(rows.rule_evaluation_eligible)
            if cohort=='CALENDAR_VERIFIED': labels=labels.where(rows.calendar_alignment_valid)
            outcomes=classify_outcomes(labels,rows.prediction,'CALL')
            fires=int((outcomes.graded & outcomes.fired).sum())
            correct=int(outcomes.correct_fire.sum()); opportunities=int(outcomes.opportunity.sum())
            precision=correct/fires if fires else np.nan
            recall=correct/opportunities if opportunities else np.nan
            result.append(dict(cohort=cohort,period=period,source_sessions=len(rows),
                eod_setups=int(rows.rule_eod_setup.sum()),all_fires=int(rows.prediction.eq('CALL').sum()),
                graded_sessions=int(outcomes.graded.sum()),graded_fires=fires,correct_fires=correct,
                precision_misses=int(outcomes.precision_miss.sum()),recall_misses=int(outcomes.recall_miss.sum()),
                opportunities=opportunities,precision=precision,recall=recall,
                f1=2*precision*recall/(precision+recall) if precision+recall>0 else (0.0 if fires and opportunities else np.nan),
                missing_gap_sessions=int(rows.rule_status.eq('UNGRADED_MISSING_GAP').sum()),
                missing_vix_sessions=int(rows.rule_status.eq('UNGRADED_MISSING_VIX').sum()),
                other_unknown_sessions=int(rows.rule_status.eq('UNGRADED_SETUP_INPUT').sum()),
                call_opportunities_ungraded_by_inputs=int((rows.actual_trade_label.isin(['CALL','BOTH']) & ~rows.rule_evaluation_eligible).sum())))
    return pd.DataFrame(result)


def backfill(start: date, end: date, destination: Path):
    from technical_analysis_service.analysis.cascade.dataset import build_base
    from technical_analysis_service.analysis.cascade.strategies import nifty_2026_call_put_research
    from technical_analysis_service.common.config import get_underlying_lookback_days, get_nifty_target_pct
    from technical_analysis_service.runtime import SERVICE_ROOT
    if get_underlying_lookback_days()!=1 or not np.isclose(get_nifty_target_pct(),.006):
        raise ValueError('This research requires the existing one-session 0.6% label configuration')
    destination.mkdir(parents=True,exist_ok=True)
    base=build_base().sort_values('signal_date').reset_index(drop=True)
    base['signal_date']=pd.to_datetime(base.signal_date).dt.strftime('%Y-%m-%d')
    _,calendar=_stored_and_calendar(start,end)
    calendar['calendar_date']=pd.to_datetime(calendar.calendar_date).dt.strftime('%Y-%m-%d')
    trading=calendar.loc[calendar.is_trading_day,'calendar_date'].tolist()
    known_dates=set(calendar.calendar_date)
    base=add_streak_history_context(base,calendar)
    base['calendar_alignment_valid']=calendar_alignment(base,trading,1)
    base['calendar_record_available']=base.signal_date.isin(known_dates)
    detail=streak_continuation_details(base)
    put=nifty_2026_call_put_research(base)['strategy_UptrendExhaustionPut_signal']
    overlay=research_conflict_signals(detail.prediction,put)
    fields=['signal_date','next_trade_date','actual_trade_label','calendar_alignment_valid',
        'calendar_record_available','history_break','open_915','close_1515','high_day','low_day',
        'recent_high_10d','recent_high_20d','ret_3d','trend_efficiency_5d',
        'breadth_advance_decline_ratio','vix_chg_pct','gap_open_atr',
        'next_open','next_high','next_low','next_close','close_1515_source']
    daily=pd.concat([base.reindex(columns=fields),detail.add_prefix('rule_'),overlay],axis=1)
    daily['prediction']=detail.prediction
    daily=daily[daily.signal_date.between(str(start),str(end))].copy()
    daily['year']=daily.signal_date.str[:4]
    for cohort in ['source','calendar_verified']:
        labels=daily.actual_trade_label.where(daily.rule_evaluation_eligible)
        if cohort=='calendar_verified': labels=labels.where(daily.calendar_alignment_valid)
        daily=pd.concat([daily,classify_outcomes(labels,daily.prediction,'CALL').add_prefix(cohort+'_')],axis=1)
    daily['target_pct']=.006; daily['stop_pct']=.004
    daily['time_exit_ist']='15:10'; daily['size_multiplier']=.5
    trades=[]
    fires=daily[daily.prediction.eq('CALL')]
    candles=load_candles(start,end) if len(fires) else pd.DataFrame()
    if not candles.empty:
        candles[candles.trade_date.astype(str).isin(fires.next_trade_date.astype(str))].to_csv(destination/'trade_candles.csv',index=False)
    groups={str(day):rows for day,rows in candles.groupby('trade_date')} if not candles.empty else {}
    for row in fires.itertuples():
        entry=float(row.next_open) if pd.notna(row.next_open) else np.nan
        result=simulate_long_trade(groups.get(str(row.next_trade_date),pd.DataFrame()),row.next_trade_date,entry)
        trades.append(dict(signal_date=row.signal_date,trade_date=row.next_trade_date,entry_price=entry,
            actual_trade_label=row.actual_trade_label,calendar_alignment_valid=row.calendar_alignment_valid,
            target_pct=.006,stop_pct=.004,time_exit_ist='15:10',size_multiplier=.5,**result))
    coverage=calendar[calendar.calendar_date.between(str(start),str(end))].copy()
    coverage=coverage.set_index('calendar_date').reindex(pd.date_range(start,end).strftime('%Y-%m-%d')).rename_axis('calendar_date').reset_index()
    coverage['calendar_record_available']=coverage.is_trading_day.notna()
    coverage['features_available']=coverage.calendar_date.isin(daily.signal_date)
    from technical_analysis_service.analysis.strategy_registry import get_strategy_registry
    notes=[('Strategy',NAME+'; registry type='+get_strategy_registry().get_meta(NAME).strategy_type+'. This diagnostic itself submits no orders or production writes.'),
        ('Scope',f'{start} through {end}; full available source history is loaded before date filtering.'),
        ('Timing','Setup uses D close features only; stored gap_open_atr on D describes D+1 open. Entry is next open, without Strategy 2 confirmation.'),
        ('Trigger','Streak only: at least three consecutive close>open sessions AND ret_3d>=1%. Breakout branch removed; recent-high features are not required.'),
        ('VIX','Available signal-day close-to-close vix_chg_pct, fraction units: 0.10 = 10%. NOT D+1 closing VIX or an observed next-open change. Missing VIX is ungraded.'),
        ('Missing gap','Every missing-gap row is ungraded, including rows without a setup. Missing setup inputs are also ungraded. Unknown input opportunities are separately counted.'),
        ('Calendar','No local 2025 calendar records. SOURCE_LABEL metrics use existing source-row labels. CALENDAR_VERIFIED metrics exclude unverifiable/misaligned rows. Missing feature sessions are not synthesized. Known missing-session boundaries reset streak and invalidate 3-day breadth windows.'),
        ('Metrics','Precision = correct graded CALL fires / graded CALL fires. Recall = correct fires / all evaluable CALL-or-BOTH labels. BOTH is a touch label, not a first-touch result or proven profitable trade.'),
        ('Conflict','This report displays the original and suppressed exhaustion PUT. Production gather_signals also applies suppression when StreakContinuationCall is a SIGNAL.'),
        ('Trade policy',json.dumps(TRADE_POLICY)),
        ('Trade replay','Underlying 5m OHLC from 09:15 entry to 15:10 bar OPEN. Missing earlier bars, entry-source mismatches and invalid bars are ungraded. If stop and target touch in one bar, report bounds, never assume a win. Special sessions without 09:15 entry remain unavailable.'),
        ('Size','0.5x is a research exposure multiplier. No fractional exchange lot is submitted. Returns are underlying returns, not option premium P&L; fees/slippage omitted.'),
        ('Label versus exit','Existing full-session +0.6% touch label is scored separately from stop-before-target or 15:10 exit. A label hit can occur after a stop or after time exit.'),
        ('Validation','Hypothesis selected after examining a known rally; retrospective results are not independent out-of-sample validation. Registry promotion is an explicit user decision.'),
    ]
    frames={'Readme':pd.DataFrame(notes,columns=['item','meaning']), 'Summary':summary_rows(daily),
        'Fired_signals':fires,'EOD_setups':daily[daily.rule_eod_setup],
        'Precision_misses':daily[daily.source_precision_miss],
        'Recall_misses':daily[daily.source_recall_miss],
        'Ungraded':daily[~daily.source_graded], 'Daily_research':daily,
        'Conflicts':daily[daily.conflict], 'Trade_replay':pd.DataFrame(trades), 'Coverage':coverage}
    workbook=destination/'StreakContinuationCall_2024_to_date.xlsx'
    with pd.ExcelWriter(workbook,engine='openpyxl') as writer:
        for name,frame in frames.items():
            frame.to_csv(destination/(name.lower()+'.csv'),index=False)
            _excel_ready(frame).to_excel(writer,sheet_name=name,index=False)
            sheet=writer.sheets[name]; sheet.freeze_panes='A2'; sheet.auto_filter.ref=sheet.dimensions
            for column in sheet.iter_cols(min_row=1,max_row=1):
                sheet.column_dimensions[column[0].column_letter].width=min(55,max(16,len(str(column[0].value))+2))
                if column[0].value in ('precision','recall','f1'):
                    for cell in sheet[column[0].column_letter][1:]: cell.number_format='0.00%'
        writer.sheets['Readme'].column_dimensions['B'].width=125
    metadata=dict(start=str(start),end=str(end),source_sessions=len(daily),
        trade_policy=TRADE_POLICY,vix_basis='SIGNAL_DAY_CLOSE_TO_CLOSE',conflict_scope='RESEARCH_OVERLAY_ONLY',
        missing_feature_dates=coverage.loc[coverage.is_trading_day.eq(True) & ~coverage.features_available,'calendar_date'].tolist(),
        missing_calendar_dates=coverage.loc[~coverage.calendar_record_available,'calendar_date'].tolist(),
        strategy_sha256=hashlib.sha256((SERVICE_ROOT/'analysis/cascade/streak_continuation.py').read_bytes()).hexdigest())
    (destination/'metadata.json').write_text(json.dumps(metadata,indent=2)+'\n',encoding='utf-8')
    # Cache only research input data so diagnostics can be reproduced without another DB fetch.
    base.to_pickle(destination/'research_input_snapshot.pkl')
    print(frames['Summary'].to_string(index=False),flush=True)
    print('Statuses: '+daily.rule_status.value_counts().to_json(),flush=True)
    print('Conflicts: '+str(int(daily.conflict.sum())),flush=True)
    if trades: print(frames['Trade_replay'].execution_status.value_counts().to_string(),flush=True)
    print(str(workbook),flush=True)
    return frames


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start',type=date.fromisoformat,default=date(2024,1,1))
    parser.add_argument('--end',type=date.fromisoformat,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args()
    if args.start>args.end: parser.error('start must not exceed end')
    backfill(args.start,args.end,args.output_dir)
