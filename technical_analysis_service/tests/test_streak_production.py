from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import Mock, patch

import pandas as pd
import pytest

from technical_analysis_service.analysis.cascade.engine import gather_signals, build_strategy_vote
from technical_analysis_service.analysis.cascade.strategies import ALL_PRODUCTION_STRATEGIES
from technical_analysis_service.analysis.cascade.option_signal_mapper import cascade_signal_detail
from technical_analysis_service.common.config import get_target_pct_for_strategy, get_sl_pct_for_strategy
from technical_analysis_service.execution.paper import (
    LiveQuote, enter_due_paper_trades, monitor_open_paper_trades, _trade_force_exit_time)
from technical_analysis_service.execution.position_sizing import size_long_option_position

IST=timezone(timedelta(hours=5,minutes=30))
NAME='StreakContinuationCall'


def test_production_catalog_abstains_for_unknown_gap():
    frame=pd.DataFrame({'gap_open_atr':[float('nan')], 'signal_date':['2026-11-09']})
    out=ALL_PRODUCTION_STRATEGIES[NAME](frame)
    assert out['strategy_'+NAME+'_signal'].tolist()==['NO_POSITION']


def test_production_conflict_and_primary_strategy_flow():
    frame=pd.DataFrame(index=[0,1])
    generators={
        'continuation':lambda df:{'strategy_'+NAME+'_signal':pd.Series(['CALL','NO_POSITION'])},
        'exhaustion':lambda df:{'strategy_UptrendExhaustionPut_signal':pd.Series(['PUT','PUT'])}}
    signals=gather_signals(frame,generators)
    assert signals['UptrendExhaustionPut'].tolist()==['NO_POSITION','PUT']
    prediction,_,_=build_strategy_vote(frame,signals)
    assert prediction.tolist()==['CALL','PUT']
    detail=cascade_signal_detail(0,frame,prediction,signals,None)
    assert detail.primary_strategy==NAME and detail.direction=='CALL'


def test_policy_is_fixed_and_half_size_uses_whole_lots():
    with patch.dict('os.environ',{'TARGET_PCT_EFFECTIVE':'3%','SL_PCT_EFFECTIVE':'5%'}):
        assert get_target_pct_for_strategy(NAME)==.006
        assert get_sl_pct_for_strategy(NAME)==.004
        assert get_target_pct_for_strategy('UptrendExhaustionPut')==.03
    assert size_long_option_position(100,65,100_000,1,size_multiplier=.5)==(7,455)
    with pytest.raises(ValueError,match='cannot fund one lot'):
        size_long_option_position(100,65,10_000,1,size_multiplier=.5)
    assert _trade_force_exit_time({'prediction_strategy':NAME,'force_exit_time':'15:15'},time(15,15))==time(15,10)


def paper_fixture():
    db=Mock()
    quote=LiveQuote(symbol='NFO:TEST',last_price=100.,quote_time=datetime(2026,11,11,9,25,tzinfo=IST),
        best_bid_price=99.,best_bid_quantity=650,best_ask_price=100.,best_ask_quantity=650,raw={})
    signal=dict(id=7,prediction_strategy=NAME,paper_trade_date=date(2026,11,11),
                option_symbol='TEST',target_1_pct=.006,stop_loss_pct=.004,lot_size=65)
    return db,quote,signal


def test_paper_entry_sizes_half_and_creates_fixed_levels():
    db,quote,signal=paper_fixture()
    db.list_paper_execution_signals.return_value=[signal]
    with patch('technical_analysis_service.execution.paper.get_settings'), \
         patch('technical_analysis_service.execution.paper.get_database_client',return_value=db), \
         patch('technical_analysis_service.execution.paper.KiteClient'), \
         patch('technical_analysis_service.execution.paper.fetch_live_option_quote',return_value=quote), \
         patch('technical_analysis_service.execution.paper._fetch_live_underlying_quote',return_value={'last_price':25000.}), \
         patch('technical_analysis_service.execution.paper.capture_paper_order_charges'), \
         patch('technical_analysis_service.common.config.get_paper_trading_capital',return_value=100_000.), \
         patch('technical_analysis_service.common.config.get_paper_capital_per_trade_pct',return_value=1.):
        result=enter_due_paper_trades(date(2026,11,11))
    assert result['opened']==1
    db.set_paper_trade_quantity.assert_called_once_with(7,455)
    args=db.create_paper_oco_orders.call_args.kwargs
    assert args['quantity']==455
    assert args['target_price']==pytest.approx(100.+.58*25000*.006)
    assert args['stop_loss_price']==pytest.approx(100.-.58*25000*.004)


def test_paper_entry_after_strategy_cutoff_creates_no_fill():
    db,quote,signal=paper_fixture()
    quote=LiveQuote(**{**quote.__dict__,'quote_time':datetime(2026,11,11,15,10,tzinfo=IST)})
    db.list_paper_execution_signals.return_value=[signal]
    with patch('technical_analysis_service.execution.paper.get_settings'), \
         patch('technical_analysis_service.execution.paper.get_database_client',return_value=db), \
         patch('technical_analysis_service.execution.paper.KiteClient'), \
         patch('technical_analysis_service.execution.paper.fetch_live_option_quote',return_value=quote):
        result=enter_due_paper_trades(date(2026,11,11))
    assert result['opened']==0 and result['failed']==1
    assert db.insert_paper_order.call_args.kwargs['status']=='FAILED'
    assert db.insert_paper_order.call_args.kwargs['filled_price'] is None
    db.open_paper_trade.assert_not_called()


def test_paper_signal_preparation_persists_strategy_specific_target_and_stop():
    from technical_analysis_service.data_manager.db.supabase_client import SupabaseDatabaseClient
    db=Mock()
    candidate=dict(trade_date=date(2026,11,9),next_trade_date=date(2026,11,11),symbol='NIFTY',
        model_version='cascade_v1',prediction_direction='CALL',selected_strategy='LONG_CALL',
        primary_strategy=NAME,primary_buy_symbol='TEST',primary_buy_token=123,
        primary_buy_option_type='CE',primary_buy_entry_price=100.,primary_buy_delta=.58,
        selection_target_1_pct=.03,quantity=65,lot_size=65,source_final_prediction='CALL',
        signal_day_close_1515=25000.)
    select=Mock(); select.fetchall.return_value=[candidate]
    insert=Mock(); insert.rowcount=1
    select_context=Mock(); select_context.__enter__=Mock(return_value=select); select_context.__exit__=Mock(return_value=False)
    insert_context=Mock(); insert_context.__enter__=Mock(return_value=insert); insert_context.__exit__=Mock(return_value=False)
    db.conn.cursor.side_effect=[select_context,insert_context]
    with patch.dict('os.environ',{'TARGET_PCT_EFFECTIVE':'3%','SL_PCT_EFFECTIVE':'5%'}):
        SupabaseDatabaseClient.prepare_paper_execution_signals(db,date(2026,11,11))
    params=insert.execute.call_args.args[1]
    assert params[7]==NAME
    assert params[14]==.006 and params[15]==.004
    assert 'COALESCE(p.effective_prediction, p.final_prediction) = o.prediction_direction' in select.execute.call_args.args[0]


def test_production_strategy_scoring_excludes_unknown_input_dates():
    from technical_analysis_service.analysis.cascade.engine import score_signal
    frame=pd.DataFrame({'signal_date':['2026-11-03','2026-11-04'],
        'actual_trade_label':['CALL','CALL'],'next_open':[100.,100.],
        'next_high':[101.,101.],'next_low':[99.9,99.9]})
    signal=pd.Series(['CALL','NO_POSITION'])
    signal.attrs['ungraded_signal_dates']=('2026-11-04',)
    result=score_signal(frame,signal,NAME)
    assert result.precision==1.0 and result.recall==1.0


@pytest.mark.parametrize('at,price,force_eod,reason',[
    (time(10),111.,True,'TARGET_HIT'),
    (time(10),89.,True,'STOP_LOSS_HIT'),
    (time(15,10),101.,False,'EOD_FORCE_EXIT')])
def test_fixed_paper_exits_do_not_ratchet(at,price,force_eod,reason):
    db,quote,signal=paper_fixture()
    signal.update(entry_price=100.,quantity=455,target_1_price=110.,stop_loss_price=90.)
    db.list_open_paper_trades.return_value=[signal]
    quote=LiveQuote(**{**quote.__dict__,'quote_time':datetime.combine(date(2026,11,11),at,tzinfo=IST),
        'last_price':price,'best_bid_price':price})
    with patch('technical_analysis_service.execution.paper.get_settings'), \
         patch('technical_analysis_service.execution.paper.get_database_client',return_value=db), \
         patch('technical_analysis_service.execution.paper.KiteClient'), \
         patch('technical_analysis_service.execution.paper.fetch_live_option_quote',return_value=quote), \
         patch('technical_analysis_service.execution.paper.capture_paper_order_charges'):
        result=monitor_open_paper_trades(force_eod_exit=force_eod)
    assert result['closed']==1 and result['cascaded']==0
    assert db.close_paper_trade.call_args.kwargs['exit_reason']==reason
    db.ratchet_paper_trade_targets.assert_not_called()
    if reason=='TARGET_HIT': assert db.fill_paper_oco_exit.call_args.kwargs['triggered_role']=='TARGET'
