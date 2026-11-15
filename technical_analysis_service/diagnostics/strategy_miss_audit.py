"""Read-only per-strategy miss audit, actual rule predicates and offline workbook."""
from __future__ import annotations

import argparse
import ast
import copy
from datetime import date, datetime, timedelta
import hashlib
import inspect
import json
from pathlib import Path
import textwrap

import numpy as np
import pandas as pd

from technical_analysis_service.runtime import output_dir


def classify_outcomes(labels: pd.Series, predictions: pd.Series, direction: str) -> pd.DataFrame:
    """Precision counts graded fires; recall counts the strategy's mandated side(s)."""
    graded = labels.isin(['CALL', 'PUT', 'BOTH', 'NO_POSITION'])
    fired = predictions.isin(['CALL', 'PUT'])
    correct = graded & (((predictions == 'CALL') & labels.isin(['CALL', 'BOTH'])) |
                        ((predictions == 'PUT') & labels.isin(['PUT', 'BOTH'])))
    opportunities = {'CALL': ['CALL', 'BOTH'], 'PUT': ['PUT', 'BOTH'],
                     'TWO_SIDED': ['CALL', 'PUT', 'BOTH']}[direction]
    opportunity = graded & labels.isin(opportunities)
    out = pd.DataFrame({'graded': graded, 'fired': fired, 'correct_fire': correct,
                        'opportunity': opportunity, 'precision_miss': graded & fired & ~correct,
                        'recall_miss': opportunity & ~correct,
                        'wrong_way': graded & (((predictions == 'CALL') & (labels == 'PUT')) |
                                               ((predictions == 'PUT') & (labels == 'CALL')))})
    return out


def _and_terms(expr):
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitAnd):
        return _and_terms(expr.left) + _and_terms(expr.right)
    return [expr]


def traced_production_rules(base: pd.DataFrame):
    """Capture the real strategy function's AND predicates, preserving nested ORs.

    Only trusted local source is compiled. No rule thresholds or trading functions
    are changed, and the captured signals are checked against the original call.
    """
    from technical_analysis_service.analysis.cascade.strategies import nifty_2026_call_put_research
    fn = nifty_2026_call_put_research
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    definition = tree.body[0]
    definition.name = '_audited_rules'
    captured = {}
    for node in ast.walk(definition):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (isinstance(key, ast.Constant) and isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name) and value.func.id == '_sig'):
                parts = []
                for expr in _and_terms(value.args[0]):
                    refs = sorted({n.id for n in ast.walk(expr) if isinstance(n, ast.Name)})
                    parts.append(ast.Tuple(elts=[ast.Constant(ast.unparse(expr)), copy.deepcopy(expr),
                        ast.Dict(keys=[ast.Constant(name) for name in refs],
                                 values=[ast.Name(id=name, ctx=ast.Load()) for name in refs])], ctx=ast.Load()))
                captured[key.value] = ast.List(elts=parts, ctx=ast.Load())
    assert len(captured) == 6, 'Production rule structure changed: update the audit tracer'
    assert isinstance(definition.body[-1], ast.Return)
    definition.body[-1].value = ast.Tuple(elts=[definition.body[-1].value,
        ast.Dict(keys=[ast.Constant(key) for key in captured], values=list(captured.values()))], ctx=ast.Load())
    namespace = dict(fn.__globals__)
    exec(compile(ast.fix_missing_locations(tree), inspect.getsourcefile(fn), 'exec'), namespace)
    signals, conditions = namespace['_audited_rules'](base)
    original = fn(base)
    for name in original:
        pd.testing.assert_series_equal(signals[name], original[name])
    return signals, conditions


def _scalar(value):
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def predicate_rows(base, indices, conditions):
    rows = []
    for key, terms in conditions.items():
        strategy = key.removeprefix('strategy_').removesuffix('_signal')
        for expression, result, refs in terms:
            for idx in indices:
                value = result.loc[idx]
                observed = {name: _scalar(ref.loc[idx] if isinstance(ref, pd.Series) else ref)
                            for name, ref in refs.items()}
                rows.append({'signal_date': base.loc[idx, 'signal_date'], 'strategy': strategy,
                             'condition': expression, 'passed': bool(value) if pd.notna(value) else False,
                             'input_missing': any(v is None or v in ('nan', 'None') for v in observed.values()),
                             'observed_values': json.dumps(observed, default=str, allow_nan=False)})
    return pd.DataFrame(rows)


def daily_price_context(base: pd.DataFrame) -> pd.DataFrame:
    out = base.copy()
    close = pd.to_numeric(out['close_1515'], errors='coerce')
    opening = pd.to_numeric(out['open_915'], errors='coerce').replace(0, np.nan)
    next_open = pd.to_numeric(out['next_open'], errors='coerce').replace(0, np.nan)
    out['previous_session_close'] = close.shift(1)
    out['signal_close_to_close_pct'] = (close / close.shift(1) - 1) * 100
    out['signal_open_to_close_pct'] = (close / opening - 1) * 100
    out['execution_gap_pct'] = (next_open / close - 1) * 100
    out['execution_open_to_close_pct'] = (pd.to_numeric(out['next_close'], errors='coerce') / next_open - 1) * 100
    out['execution_max_up_pct'] = (pd.to_numeric(out['future_high_nd'], errors='coerce') / next_open - 1) * 100
    out['execution_max_down_pct'] = (1 - pd.to_numeric(out['future_low_nd'], errors='coerce') / next_open) * 100
    return out


def calendar_alignment(base: pd.DataFrame, trading_days: list[str], horizon: int) -> pd.Series:
    """A shifted feature row must actually be the next exchange session(s)."""
    dates=base['signal_date'].tolist()
    positions={day:i for i,day in enumerate(trading_days)}
    valid=[]
    for i,day in enumerate(dates):
        pos=positions.get(day)
        expected=trading_days[pos+1:pos+1+horizon] if pos is not None else []
        actual=dates[i+1:i+1+horizon]
        valid.append(len(expected)==horizon and actual==expected)
    return pd.Series(valid,index=base.index,dtype=bool)


def _stored_and_calendar(start, end):
    from technical_analysis_service.common.config import get_settings
    from technical_analysis_service.data_manager.db.client_factory import get_database_client
    db = get_database_client(get_settings()); db.connect()
    try:
        frames = []
        with db.conn.cursor() as cur:
            cur.execute('SET TRANSACTION READ ONLY')
            for sql, params in [
                ('SELECT signal_date,final_prediction,effective_prediction,primary_strategy,event_gate_reason,gap_gate_reason FROM "NiftyPrediction" WHERE symbol=%s AND model_version=%s AND signal_date BETWEEN %s AND %s ORDER BY signal_date', ('NIFTY','cascade_v1',start,end)),
                ('SELECT calendar_date,is_trading_day,is_special_session,notes FROM "TradingCalendar" WHERE exchange=%s AND calendar_date BETWEEN %s AND %s ORDER BY calendar_date', ('NSE',start,end+timedelta(days=30))),
            ]:
                cur.execute(sql, params)
                frames.append(pd.DataFrame(cur.fetchall(), columns=[c[0] for c in cur.description]))
        return frames
    finally:
        db.close()


def build_audit(start: date, end: date, focus_start: date):
    from technical_analysis_service.analysis.cascade.dataset import build_base
    from technical_analysis_service.analysis.cascade.engine import gather_signals
    from technical_analysis_service.analysis.cascade.strategies import ALL_PRODUCTION_STRATEGIES
    from technical_analysis_service.analysis.strategy_registry import get_strategy_registry
    from technical_analysis_service.backtest.vectorbt_research.strategy_grid import DEFAULT_VARIANTS
    from technical_analysis_service.common.config import get_underlying_lookback_days

    base = build_base().sort_values('signal_date').reset_index(drop=True)
    from technical_analysis_service.analysis.cascade.streak_continuation import add_streak_history_context
    base = add_streak_history_context(base)
    base['signal_date'] = pd.to_datetime(base['signal_date']).dt.strftime('%Y-%m-%d')
    base = daily_price_context(base)
    stored, calendar = _stored_and_calendar(start, end)
    trading_days=pd.to_datetime(calendar.loc[calendar['is_trading_day'],'calendar_date']).dt.strftime('%Y-%m-%d').tolist()
    base['calendar_alignment_valid']=calendar_alignment(base,trading_days,get_underlying_lookback_days())
    base['scoring_label']=base['actual_trade_label'].where(base['calendar_alignment_valid'])
    base['data_status']='FEATURES_AVAILABLE'
    base['label_status']=np.where(base['actual_trade_label'].isna(),'PENDING_FUTURE_DATA',
        np.where(base['calendar_alignment_valid'],'GRADED','EXCLUDED_CALENDAR_GAP'))
    stored['signal_date'] = pd.to_datetime(stored['signal_date']).dt.strftime('%Y-%m-%d')
    stored = stored.rename(columns={c:'stored_'+c for c in stored if c != 'signal_date'})
    base = base.merge(stored, on='signal_date', how='left', validate='one_to_one')
    mask = base['signal_date'].between(start.isoformat(), end.isoformat())
    selected = base.loc[mask].copy()
    registry = get_strategy_registry()
    prod = gather_signals(base, ALL_PRODUCTION_STRATEGIES)
    research = {variant.name: variant.signal_fn(base) for variant in DEFAULT_VARIANTS}
    signals = {**prod, **research}
    _, traced = traced_production_rules(base)
    predicates = predicate_rows(base, selected.index, traced)
    failures = predicates[~predicates['passed']]
    failed = failures.groupby(['signal_date','strategy'])['condition'].agg('; '.join).to_dict()
    daily = selected.copy()
    parts = []
    price_fields = ['signal_date','next_trade_date','open_915','close_1515','next_open','next_close',
                    'signal_close_to_close_pct','signal_open_to_close_pct','execution_gap_pct',
                    'execution_open_to_close_pct','execution_max_up_pct','execution_max_down_pct',
                    'actual_trade_label','scoring_label','label_status','calendar_alignment_valid','data_status',
                    'actual_trade_label_relaxed','rsi5','ret_3d','ma20','ma50',
                    'ma20_slope','gap_open_atr','oi_regime','macro_risk_off',
                    'stored_final_prediction','stored_effective_prediction','stored_primary_strategy',
                    'stored_event_gate_reason','stored_gap_gate_reason']
    for name, signal in signals.items():
        meta = registry.get_meta(name)
        pred = signal.reindex(selected.index)
        unknown_dates = signal.attrs.get('ungraded_signal_dates', ())
        if unknown_dates:
            pred = pred.mask(selected.signal_date.astype(str).isin(unknown_dates))
        daily['strategy_'+name] = pred
        part = selected.reindex(columns=price_fields).copy()
        part.insert(2,'strategy',name)
        part.insert(3,'strategy_type',meta.strategy_type)
        part.insert(4,'mandated_direction',meta.direction)
        part['prediction'] = pred
        scoring_label = selected['scoring_label'].where(pred.notna())
        part['scoring_label'] = scoring_label
        part['strategy_input_status'] = np.where(pred.notna(),'EVALUATED','UNGRADED_MISSING_INPUT')
        part = pd.concat([part,classify_outcomes(scoring_label,pred,meta.direction)],axis=1)
        part['failed_conditions'] = [failed.get((str(day),name),'') for day in part['signal_date']]
        parts.append(part)
    long = pd.concat(parts,ignore_index=True)
    expected_days=[day for day in trading_days if start.isoformat()<=day<=end.isoformat()]
    missing_days=sorted(set(expected_days)-set(daily['signal_date']))
    if missing_days:
        missing_daily=pd.DataFrame({'signal_date':missing_days,'data_status':'MISSING_FEATURES',
                                    'label_status':'MISSING_FEATURES','calendar_alignment_valid':False})
        daily=pd.concat([daily,missing_daily],ignore_index=True).sort_values('signal_date')
        extra=[]
        for name in signals:
            meta=registry.get_meta(name)
            for day in missing_days:
                extra.append({'signal_date':day,'strategy':name,'strategy_type':meta.strategy_type,
                    'mandated_direction':meta.direction,'data_status':'MISSING_FEATURES','label_status':'MISSING_FEATURES',
                    'prediction':pd.NA,'calendar_alignment_valid':False,
                    'graded':False,'fired':False,'correct_fire':False,'opportunity':False,
                    'precision_miss':False,'recall_miss':False,'wrong_way':False,
                    'failed_conditions':'Not evaluated: source feature row missing'})
        long=pd.concat([long,pd.DataFrame(extra)],ignore_index=True).sort_values(['signal_date','strategy'])
    summaries = []
    for (name,kind,direction), group in long.groupby(['strategy','strategy_type','mandated_direction']):
        graded_fires=int((group['graded'] & group['fired']).sum())
        correct=int(group['correct_fire'].sum()); opp=int(group['opportunity'].sum())
        summaries.append({'strategy':name,'strategy_type':kind,'mandated_direction':direction,
            'sessions_in_scope':len(group),'sessions_evaluated':int(group['data_status'].eq('FEATURES_AVAILABLE').sum()),
            'missing_feature_sessions':int(group['data_status'].eq('MISSING_FEATURES').sum()),
            'graded_sessions':int(group['graded'].sum()),
            'ungraded_sessions':int((~group['graded']).sum()),'all_fires':int(group['fired'].sum()),
            'graded_fires':graded_fires,'correct_fires':correct,'precision_misses':int(group['precision_miss'].sum()),
            'recall_misses':int(group['recall_miss'].sum()),'opportunities':opp,
            'precision':correct/graded_fires if graded_fires else np.nan,
            'recall':correct/opp if opp else np.nan,'wrong_way_fires':int(group['wrong_way'].sum())})
    coverage = calendar.copy()
    coverage['calendar_date'] = pd.to_datetime(coverage['calendar_date']).dt.strftime('%Y-%m-%d')
    coverage=coverage[coverage['calendar_date'].between(start.isoformat(),end.isoformat())].copy()
    coverage['features_available'] = coverage['calendar_date'].isin(selected['signal_date'])
    coverage['stored_prediction_available'] = coverage['calendar_date'].isin(stored['signal_date'])
    coverage['calendar_source_disagreement'] = coverage['features_available'] & ~coverage['is_trading_day']
    focus = daily[daily['signal_date'] >= focus_start.isoformat()].copy()
    # Session-day attribution: the decision for a session is on its preceding signal row.
    execution_columns = ['signal_date','next_trade_date','stored_final_prediction','stored_effective_prediction']
    prior = base.reindex(columns=execution_columns).rename(columns={
        'signal_date':'preceding_signal_date','next_trade_date':'session_date',
        'stored_final_prediction':'preceding_stored_final','stored_effective_prediction':'preceding_stored_effective'})
    prior['session_date'] = pd.to_datetime(prior['session_date'],errors='coerce').dt.strftime('%Y-%m-%d')
    sessions = focus[['signal_date','open_915','close_1515','signal_close_to_close_pct','signal_open_to_close_pct']].rename(columns={'signal_date':'session_date'})
    sessions = sessions.merge(prior,on='session_date',how='left',validate='one_to_one')
    return {'Daily_2026':daily,'Strategy_day':long,'Strategy_summary':pd.DataFrame(summaries),
            'Precision_misses':long[long['precision_miss']].copy(),
            'Recall_misses':long[long['recall_miss']].copy(), 'Rule_conditions':predicates,
            'Rally_signals':focus,'Rally_sessions':sessions,'Calendar_coverage':coverage}


def _excel_ready(frame):
    return frame.apply(lambda col: col.map(lambda value: value.isoformat() if isinstance(value,(datetime,date,pd.Timestamp)) else value))


def export(start, end, focus_start, destination, metrics_file=None):
    from technical_analysis_service.common.config import get_underlying_lookback_days, get_nifty_target_pct
    if start > end:
        raise ValueError('start must be on or before end')
    destination.mkdir(parents=True,exist_ok=True)
    frames = build_audit(start,end,focus_start)
    if metrics_file:
        frames['Production_features'] = pd.read_csv(metrics_file)
        metrics_dates = pd.to_datetime(frames['Production_features']['signal_date']).dt.strftime('%Y-%m-%d')
        source_dates=frames['Daily_2026'].loc[frames['Daily_2026']['data_status'].eq('FEATURES_AVAILABLE'),'signal_date']
        if set(metrics_dates) != set(source_dates):
            raise ValueError('The reused production metrics file does not match the audit date coverage')
    else:
        from .export_production_strategy_metrics_log import build_production_strategy_metrics_log
        frames['Production_features'] = build_production_strategy_metrics_log(start,end)
    notes = [
        ('Scope',f'Available source sessions from {start} through {end}; no future sessions are manufactured.'),
        ('Reconstruction','Raw strategy votes are recomputed with current code/configuration and repaired data. They are not immutable records of what ran live on those dates.'),
        ('Stored decisions','stored_* columns come from NiftyPrediction; an empty stored decision means no stored row, not NO_POSITION.'),
        ('Dates','signal_date D uses its close/features plus available next-session opening context; next_trade_date D+1 is the execution/outcome session. Rally_sessions aligns preceding decisions to the moved session.'),
        ('Label horizon',str(get_underlying_lookback_days())+' session(s) after signal_date; entry reference is next_open.'),
        ('Strict target',str(get_nifty_target_pct())+' as a decimal fraction; BOTH means both barriers touched, not a known first-touch order.'),
        ('Returns','New *_pct movement columns use percent units, e.g. 1.5 means 1.5%; existing raw feature returns retain their original units. Close-to-close changes use consecutive available source rows; inspect Calendar_coverage around gaps.'),
        ('Prices','close_1515 is the stored feature close, with close_1515_source identifying its source; not always an official exchange closing price.'),
        ('Precision','Correct graded fires / all graded fires. NULL labels are excluded.'),
        ('Recall','Correct graded fires / opportunities for the mandated direction: CALL uses CALL/BOTH; PUT uses PUT/BOTH; TWO_SIDED uses any directional label.'),
        ('Miss overlap','An opposite-side two-sided vote can be both a precision and recall miss. These counts are not disjoint.'),
        ('Missing data','Calendar_coverage exposes missing feature/prediction rows. Rule_conditions reports predicate truth and missing referenced inputs separately.'),
        ('Calendar gaps','Missing feature sessions are explicit unscored rows. actual_trade_label preserves the engine label; scoring_label is NULL when the feature-row shift skips an expected exchange session, so precision/recall excludes those labels.'),
        ('Predicates','Compiled from trusted local production-rule source; predicates preserve nested ORs and reproduced votes are asserted equal to the original function.'),
        ('Research','RESEARCH strategies cannot create a production decision. Their rows here are for comparison.'),
        ('Use','Offline diagnostics. Outcome/context columns include hindsight information and must not be used as contemporaneous trading inputs.'),
        ('Changes','Read-only against the trading database. No production strategy, threshold, guard or paper/live order is changed.'),
    ]
    frames={'Readme':pd.DataFrame(notes,columns=['item','meaning']),**frames}
    for name,frame in frames.items():
        frame.to_csv(destination/(name.lower()+'.csv'),index=False)
    workbook=destination/'NIFTY_2026_strategy_miss_analysis.xlsx'
    with pd.ExcelWriter(workbook,engine='openpyxl') as writer:
        for name,frame in frames.items():
            _excel_ready(frame).to_excel(writer,sheet_name=name,index=False)
            sheet=writer.sheets[name]
            sheet.freeze_panes='A2'; sheet.auto_filter.ref=sheet.dimensions
            for col in sheet.iter_cols(min_row=1,max_row=1):
                label=str(col[0].value)
                sheet.column_dimensions[col[0].column_letter].width=min(65,max(15,len(label)+2))
                if label in {'precision','recall'}:
                    for cell in sheet[col[0].column_letter][1:]: cell.number_format='0.0%'
        writer.sheets['Readme'].column_dimensions['B'].width=115
    code_files=['analysis/cascade/strategies.py','analysis/cascade/engine.py','analysis/cascade/pipeline.py','analysis/strategy_registry.yaml','diagnostics/strategy_miss_audit.py']
    from technical_analysis_service.runtime import SERVICE_ROOT
    metadata={'start':str(start),'end':str(end),'latest_source_session':str(frames['Daily_2026']['signal_date'].max()),
              'sessions_in_scope':len(frames['Daily_2026']),
              'source_sessions':int(frames['Daily_2026']['data_status'].eq('FEATURES_AVAILABLE').sum()),
              'strategy_days':len(frames['Strategy_day']),
              'strategies':len(frames['Strategy_summary']),'label_horizon':get_underlying_lookback_days(),
              'target_fraction':get_nifty_target_pct(),
              'missing_feature_sessions':frames['Calendar_coverage'].query('is_trading_day and not features_available')['calendar_date'].tolist(),
              'labels_excluded_for_calendar_gaps':frames['Daily_2026'].loc[frames['Daily_2026']['label_status'].eq('EXCLUDED_CALENDAR_GAP'),'signal_date'].tolist(),
              'calendar_source_disagreements':frames['Calendar_coverage'].loc[frames['Calendar_coverage']['calendar_source_disagreement'],'calendar_date'].tolist(),
              'code_sha256':{name:hashlib.sha256((SERVICE_ROOT/name).read_bytes()).hexdigest() for name in code_files}}
    (destination/'metadata.json').write_text(json.dumps(metadata,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'workbook':str(workbook),**metadata},indent=2),flush=True)
    return frames


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start',required=True,type=date.fromisoformat)
    parser.add_argument('--end',required=True,type=date.fromisoformat)
    parser.add_argument('--focus-start',type=date.fromisoformat,default=date(2026,10,28))
    parser.add_argument('--output-dir',type=Path,default=output_dir()/'analysis/strategy_misses')
    parser.add_argument('--metrics-file',type=Path,help='Reuse a production_strategy_metrics_log CSV exported for the same window')
    args=parser.parse_args()
    export(args.start,args.end,args.focus_start,args.output_dir,args.metrics_file)


if __name__=='__main__':
    main()
