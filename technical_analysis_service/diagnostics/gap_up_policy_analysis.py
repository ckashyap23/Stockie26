"""Offline paired GAP_UP policy comparison; never changes production records."""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .strategy_miss_audit import (_stored_and_calendar, calendar_alignment,
                                 classify_outcomes, _excel_ready)
from .streak_continuation_backtest import load_candles, simulate_long_trade

POLICIES=['KEEP_VETO','REMOVE_GAP_UP','HALF_NORMAL_ON_GAP_UP','CAP_HALF_STANDARD_ON_GAP_UP']


def apply_policies(post_event, gap_up, gap_down, normal_size):
    """All policies retain the PUT gap-down veto; only CALL gap-up differs."""
    allowed=post_event.mask(post_event.eq('PUT') & gap_down,'NO_POSITION')
    affected=allowed.eq('CALL') & gap_up
    result={}
    for policy in POLICIES:
        prediction=allowed.mask(affected,'NO_POSITION') if policy=='KEEP_VETO' else allowed.copy()
        size=normal_size.where(prediction.isin(['CALL','PUT']),0.).copy()
        if policy=='HALF_NORMAL_ON_GAP_UP': size.loc[affected]*=.5
        if policy=='CAP_HALF_STANDARD_ON_GAP_UP': size.loc[affected]=size.loc[affected].clip(upper=.5)
        result[policy]=(prediction,size)
    return affected,result


def drawdown(contributions):
    equity=np.r_[0.,pd.to_numeric(contributions,errors='coerce').fillna(0.).cumsum().to_numpy()]
    return float((np.maximum.accumulate(equity)-equity).max())


def run(start: date,end: date,destination: Path):
    from technical_analysis_service.analysis.cascade.dataset import build_base,scoring_frame
    from technical_analysis_service.analysis.cascade.engine import gather_signals,build_cascade,walk_forward
    from technical_analysis_service.analysis.cascade.strategies import ALL_PRODUCTION_STRATEGIES
    from technical_analysis_service.analysis.cascade.option_signal_mapper import enrich_option_signal_columns
    from technical_analysis_service.analysis.cascade.streak_continuation import add_streak_history_context
    from technical_analysis_service.analysis.cascade.pipeline import _apply_event_gate,_apply_guard_layer
    from technical_analysis_service.common.config import get_gap_guard_pct,get_underlying_lookback_days
    from technical_analysis_service.runtime import SERVICE_ROOT

    destination.mkdir(parents=True,exist_ok=True)
    base=build_base().sort_values('signal_date').reset_index(drop=True)
    base['signal_date']=pd.to_datetime(base.signal_date).dt.strftime('%Y-%m-%d')
    _,calendar=_stored_and_calendar(start,end)
    calendar['calendar_date']=pd.to_datetime(calendar.calendar_date).dt.strftime('%Y-%m-%d')
    base=add_streak_history_context(base,calendar)
    base=base[base.signal_date.ge('2024-01-01')].reset_index(drop=True)
    signals=gather_signals(base,ALL_PRODUCTION_STRATEGIES)
    resolved=base[base.next_open.notna()]
    raw,precisions=build_cascade(base,signals,scoring_frame(resolved))
    post_event=_apply_event_gate(base,raw)
    with_meta=enrich_option_signal_columns(base,post_event,signals,precisions)
    threshold=get_gap_guard_pct()
    gap=base.next_open/base.close_1515-1
    up=gap.gt(threshold).fillna(False)
    down=(base.close_1515/base.next_open-1).gt(threshold).fillna(False)
    normal=pd.Series(np.where(with_meta.primary_strategy.eq('StreakContinuationCall'),.5,1.),index=base.index)
    affected,variants=apply_policies(post_event,up,down,normal)
    # Global CALL gap-up veto was removed on 11 November 2026. KEEP_VETO
    # remains a historical counterfactual for reproducible policy comparisons.
    actual=_apply_guard_layer(base.copy(),raw,signals)
    pd.testing.assert_series_equal(variants['REMOVE_GAP_UP'][0],actual,check_names=False)
    trading=calendar.loc[calendar.is_trading_day,'calendar_date'].tolist()
    base['calendar_alignment_valid']=calendar_alignment(base,trading,get_underlying_lookback_days())
    mask=base.signal_date.between(str(start),str(end))
    fields=['signal_date','next_trade_date','actual_trade_label','calendar_alignment_valid','close_1515',
        'next_open','next_high','next_low','next_close','gap_open_atr','vix_chg_pct']
    daily=base.reindex(columns=fields).copy()
    daily['year']=base.signal_date.str[:4]
    daily['raw_prediction']=raw; daily['post_event_prediction']=post_event
    daily['primary_strategy_before_gap']=with_meta.primary_strategy
    daily['opening_gap_pct']=gap*100
    daily['affected_gap_up_call']=affected
    daily['normal_size_standard_multiple']=normal
    close_return=base.next_close/base.next_open-1
    summary=[]; proxies=[]
    for policy,(prediction,size) in variants.items():
        daily[policy+'_prediction']=prediction
        daily[policy+'_size']=size
        direction=pd.Series(np.select([prediction.eq('CALL'),prediction.eq('PUT')],[1.,-1.],default=0.),index=base.index)
        daily[policy+'_close_markout_pct']=close_return*direction*size*100
        daily.loc[direction.eq(0),policy+'_close_markout_pct']=0.
        for population in ['ALL_PRODUCTION','STREAK_PRIMARY_ONLY']:
            pred=prediction if population=='ALL_PRODUCTION' else prediction.where(with_meta.primary_strategy.eq('StreakContinuationCall'),'NO_POSITION')
            for cohort in ['SOURCE_LABEL','CALENDAR_VERIFIED']:
                labels=base.actual_trade_label.copy()
                if population=='STREAK_PRIMARY_ONLY':
                    unknown=signals['StreakContinuationCall'].attrs.get('ungraded_signal_dates',())
                    labels=labels.where(~base.signal_date.isin(unknown))
                if cohort=='CALENDAR_VERIFIED': labels=labels.where(base.calendar_alignment_valid)
                selected=daily.loc[mask]
                for period,idx in [('ALL',selected.index),*[(year,group.index) for year,group in selected.groupby('year')]]:
                    outcome=classify_outcomes(labels.loc[idx],pred.loc[idx],'TWO_SIDED' if population=='ALL_PRODUCTION' else 'CALL')
                    n=int((outcome.graded & outcome.fired).sum()); correct=int(outcome.correct_fire.sum()); opp=int(outcome.opportunity.sum())
                    summary.append(dict(population=population,cohort=cohort,period=period,policy=policy,
                        graded_sessions=int(outcome.graded.sum()),fires=n,correct=correct,
                        precision_misses=int(outcome.precision_miss.sum()),recall_misses=int(outcome.recall_miss.sum()),
                        opportunities=opp,precision=correct/n if n else np.nan,recall=correct/opp if opp else np.nan))
        for period,rows in [('ALL',daily.loc[mask]),*list(daily.loc[mask].groupby('year'))]:
            valid=rows.actual_trade_label.notna() & rows.next_open.notna() & rows.next_close.notna()
            markout=rows.loc[valid,policy+'_close_markout_pct']
            proxies.append(dict(period=period,policy=policy,
                method='DIRECTIONAL_NEXT_OPEN_TO_FEATURE_CLOSE_NOT_OPTION_PNL',
                evaluable_sessions=int(valid.sum()),sized_return_sum_pct=float(markout.sum()),
                max_drawdown_return_units_pct=drawdown(markout),
                allocated_standard_units=float(rows.loc[valid,policy+'_size'].sum())))

    daily=daily.loc[mask].copy()
    blocked=daily[daily.affected_gap_up_call].copy()
    blocked['label_call_hit']=blocked.actual_trade_label.isin(['CALL','BOTH'])
    blocked['max_up_pct']=(blocked.next_high/blocked.next_open-1)*100
    blocked['max_down_pct']=(1-blocked.next_low/blocked.next_open)*100
    blocked['open_to_feature_close_pct']=(blocked.next_close/blocked.next_open-1)*100

    # Realistic stop/target ordering for the promoted fixed-exit strategy only.
    streak_fires=daily[daily.primary_strategy_before_gap.eq('StreakContinuationCall') & daily.post_event_prediction.eq('CALL')]
    candles=load_candles(start,end) if len(streak_fires) else pd.DataFrame()
    groups={str(day):rows for day,rows in candles.groupby('trade_date')} if len(candles) else {}
    replay=[]
    for row in streak_fires.itertuples():
        trade=simulate_long_trade(groups.get(str(row.next_trade_date),pd.DataFrame()),row.next_trade_date,float(row.next_open))
        for policy in POLICIES:
            size=getattr(row,policy+'_size')
            lower=trade.get('underlying_return_lower',np.nan)
            upper=trade.get('underlying_return_upper',np.nan)
            replay.append(dict(signal_date=row.signal_date,trade_date=row.next_trade_date,year=row.year,
                policy=policy,size=size,opening_gap_pct=row.opening_gap_pct,
                actual_label=row.actual_trade_label,affected_gap_up_call=row.affected_gap_up_call,
                execution_status='VETOED' if size==0 else trade['execution_status'],
                sized_return_lower_pct=0. if size==0 else lower*size*100,
                sized_return_upper_pct=0. if size==0 else upper*size*100))
    replay=pd.DataFrame(replay)
    replay_summary=[]
    if len(replay):
        for (period,policy),rows in pd.concat([replay.assign(period='ALL'),replay.assign(period=replay.year)]).groupby(['period','policy']):
            valid=rows.sized_return_lower_pct.notna()
            replay_summary.append(dict(period=period,policy=policy,candidate_signals=len(rows),
                admitted_trades=int(rows['size'].gt(0).sum()),graded_trade_replays=int((valid & rows['size'].gt(0)).sum()),
                target_exits=int(rows.execution_status.eq('TARGET').sum()),
                stop_exits=int(rows.execution_status.isin(['STOP','STOP_GAP']).sum()),
                time_exits=int(rows.execution_status.eq('TIME_EXIT').sum()),
                sized_return_lower_sum_pct=rows.loc[valid,'sized_return_lower_pct'].sum(),
                sized_return_upper_sum_pct=rows.loc[valid,'sized_return_upper_pct'].sum(),
                max_drawdown_lower_pct=drawdown(rows.sized_return_lower_pct)))

    # A second signal cohort uses trailing-window decisions, retaining all guards.
    resolved=base.loc[resolved.index]
    wf=walk_forward(resolved, {name:sig.reindex(resolved.index) for name,sig in signals.items()})
    wf_event=_apply_event_gate(resolved.copy(),wf)
    _,wf_policies=apply_policies(wf_event,up.reindex(resolved.index),down.reindex(resolved.index),normal.reindex(resolved.index))
    wf_rows=[]
    # The first WF_WINDOW observations have no walk-forward decision.
    from technical_analysis_service.analysis.cascade.constants import WF_WINDOW
    eligible=resolved.iloc[WF_WINDOW:]
    for policy,(pred,_) in wf_policies.items():
        for cohort in ['SOURCE_LABEL','CALENDAR_VERIFIED']:
            labels=eligible.actual_trade_label.copy()
            if cohort=='CALENDAR_VERIFIED': labels=labels.where(eligible.calendar_alignment_valid)
            outcome=classify_outcomes(labels,pred.reindex(eligible.index),'TWO_SIDED')
            n=int((outcome.graded & outcome.fired).sum()); correct=int(outcome.correct_fire.sum()); opp=int(outcome.opportunity.sum())
            wf_rows.append(dict(policy=policy,cohort=cohort,fires=n,correct=correct,
                precision=correct/n if n else np.nan,recall=correct/opp if opp else np.nan,
                precision_misses=int(outcome.precision_miss.sum()),recall_misses=int(outcome.recall_miss.sum())))

    notes=[('Scope',f'{start} through {end}; current production strategy roster, source sessions through '+str(daily.signal_date.max())),
        ('Threshold',f'GAP_UP means next_open / signal close_1515 - 1 > {threshold:.2%}; equality is allowed.'),
        ('Pairing','Keep veto; remove CALL GAP_UP only; admit GAP_UP CALLs at half normal strategy allocation; or cap their allocation at 0.5x standard. Event and PUT GAP_DOWN guards remain identical.'),
        ('Sizing','Normal size is 0.5x standard for StreakContinuationCall and 1x for other primaries. HALF_NORMAL makes streak GAP_UP entries 0.25x. CAP_HALF_STANDARD leaves streak at 0.5x. Real option lots round down and may leave no trade; continuous sizes here do not model that rounding.'),
        ('Metrics','Correct CALL/BOTH for CALL, PUT/BOTH for PUT. Half size has identical signal precision/recall to removing the veto because admission is identical. STREAK_PRIMARY_ONLY excludes unknown-input dates and uses all remaining CALL/BOTH opportunities, not merely firing days. ALL_PRODUCTION keeps dates evaluable by other strategies.'),
        ('In-sample','Main rows replay the current full-history chooser, whose conflict ranking uses full-history precision. All policy comparisons share the same upstream decisions; this is paired retrospective sensitivity analysis.'),
        ('Walk-forward','Walk_forward_labels uses existing trailing-window chooser after its warmup, then the same event/gap policy comparison. It is not independent validation of a strategy chosen after inspecting the rally.'),
        ('Markout proxy','Directional next-open to next feature-close return times allocation; sums are return units per fixed standard underlying notional, NOT compounded portfolio return, rupee returns, option premium P&L or the actual cascading paper exits. PUT profit is approximated as negative underlying return.'),
        ('Streak trade replay','Only Streak-primary signals: underlying 5m replay with +0.6% target, -0.4% stop and 15:10 exit; same-bar ambiguity is bounded. Paper uses delta-adjusted options at actual later quotes. No option IV/theta, fees, slippage or integer lot sizing is inferred.'),
        ('Calendar','Local calendar lacks 2025 records and six 2026 feature sessions. SOURCE_LABEL uses existing engine labels; CALENDAR_VERIFIED excludes unverifiable/skipped exchange sessions. Neither manufactures missing data.'),
        ('Execution','Read-only database access; production thresholds, predictions, paper trades and scheduler settings are unchanged.'),
    ]
    frames={'Readme':pd.DataFrame(notes,columns=['item','meaning']),'Signal_metrics':pd.DataFrame(summary),
        'Walk_forward_labels':pd.DataFrame(wf_rows),'Markout_proxy':pd.DataFrame(proxies),
        'Blocked_CALLs':blocked,'Streak_trade_summary':pd.DataFrame(replay_summary),
        'Streak_trade_replay':replay,'Daily_policies':daily,'Calendar':calendar}
    workbook=destination/'GAP_UP_policy_comparison.xlsx'
    with pd.ExcelWriter(workbook,engine='openpyxl') as writer:
        for name,frame in frames.items():
            frame.to_csv(destination/(name.lower()+'.csv'),index=False)
            _excel_ready(frame).to_excel(writer,sheet_name=name,index=False)
            sheet=writer.sheets[name];sheet.freeze_panes='A2';sheet.auto_filter.ref=sheet.dimensions
            for col in sheet.iter_cols(min_row=1,max_row=1):
                sheet.column_dimensions[col[0].column_letter].width=min(55,max(16,len(str(col[0].value))+2))
                if col[0].value in ['precision','recall']:
                    for cell in sheet[col[0].column_letter][1:]:cell.number_format='0.00%'
        writer.sheets['Readme'].column_dimensions['B'].width=120
    metadata=dict(start=str(start),end=str(end),latest_source=str(daily.signal_date.max()),
        gap_threshold=threshold,affected_calls=len(blocked),production_changed=False,
        strategy_hash=hashlib.sha256((SERVICE_ROOT/'analysis/cascade/strategies.py').read_bytes()).hexdigest())
    (destination/'metadata.json').write_text(json.dumps(metadata,indent=2)+'\n',encoding='utf-8')
    print(frames['Signal_metrics'].query("population=='ALL_PRODUCTION' and cohort=='SOURCE_LABEL' and period=='ALL'").to_string(index=False),flush=True)
    print('Blocked calls:\n'+blocked[['signal_date','primary_strategy_before_gap','opening_gap_pct','actual_trade_label','open_to_feature_close_pct']].to_string(index=False),flush=True)
    print('Streak replay:\n'+frames['Streak_trade_summary'].to_string(index=False),flush=True)
    print('Walk forward:\n'+frames['Walk_forward_labels'].to_string(index=False),flush=True)
    print(str(workbook),flush=True)
    return frames


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start',type=date.fromisoformat,default=date(2024,1,1))
    parser.add_argument('--end',type=date.fromisoformat,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args()
    if args.start>args.end: parser.error('start must not exceed end')
    run(args.start,args.end,args.output_dir)
