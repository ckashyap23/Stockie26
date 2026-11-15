import pandas as pd
import pytest

from technical_analysis_service.analysis.cascade.streak_continuation import (
    streak_continuation_details, research_conflict_signals)
from technical_analysis_service.analysis.strategy_registry import get_strategy_registry


def frame():
    return pd.DataFrame(dict(signal_date=pd.date_range('2026-01-01',periods=4),
        close_1515=[102.,103.,104.,105.], open_915=[101.,102.,103.,104.],
        high_day=[102.5,103.5,104.5,105.5], low_day=[100.,101.,102.,103.],
        recent_high_10d=[200.]*4, recent_high_20d=[200.]*4,
        breadth_advance_decline_ratio=[1.5]*4, trend_efficiency_5d=[.6]*4,
        ret_3d=[.01]*4, gap_open_atr=[0.]*4, vix_chg_pct=[.1]*4))


def test_streak_requires_three_sessions_and_all_boundaries_are_inclusive():
    out = streak_continuation_details(frame())
    assert out.streak.tolist()==[1,2,3,4]
    assert out.prediction.iloc[2:].tolist()==['CALL','CALL']


def test_breakout_alone_cannot_trigger_and_high_features_are_not_required():
    f=frame(); f.loc[2,'ret_3d']=0.; f.loc[1,'recent_high_10d']=103.
    f.loc[2,'recent_high_10d']=200.
    assert streak_continuation_details(f).prediction.iloc[2]=='NO_POSITION'
    f=frame().drop(columns=['recent_high_10d','recent_high_20d'])
    assert streak_continuation_details(f).prediction.iloc[2]=='CALL'


@pytest.mark.parametrize('gap,expected',[(-.5,'CALL'),(1.5,'CALL'),(-.50001,'NO_POSITION'),(1.50001,'NO_POSITION')])
def test_gap_guards(gap,expected):
    f=frame(); f.loc[2,'gap_open_atr']=gap
    assert streak_continuation_details(f).prediction.iloc[2]==expected


def test_missing_gap_is_ungraded_even_on_no_setup_day():
    f=frame(); f.loc[2,'gap_open_atr']=None; f.loc[2,'ret_3d']=0.
    out=streak_continuation_details(f)
    assert pd.isna(out.prediction.iloc[2])
    assert out.status.iloc[2]=='UNGRADED_MISSING_GAP'
    assert not out.evaluation_eligible.iloc[2]


def test_vix_units_missing_and_breadth_average():
    f=frame(); f.loc[2,'vix_chg_pct']=.10001
    assert streak_continuation_details(f).prediction.iloc[2]=='NO_POSITION'
    f.loc[2,'vix_chg_pct']=None
    assert pd.isna(streak_continuation_details(f).prediction.iloc[2])
    f=frame(); f.loc[0,'breadth_advance_decline_ratio']=1.0
    assert streak_continuation_details(f).prediction.iloc[2]=='NO_POSITION'


def test_zero_range_and_missing_session_do_not_create_setup():
    f=frame(); f.loc[2,'high_day']=f.loc[2,'low_day']
    assert pd.isna(streak_continuation_details(f).prediction.iloc[2])
    f=frame(); f['history_break']=[False,False,True,False]
    out=streak_continuation_details(f)
    assert out.streak.tolist()==[1,2,1,2]
    assert pd.isna(out.prediction.iloc[2]) and pd.isna(out.prediction.iloc[3])


def test_future_prices_do_not_change_past_decisions():
    f=frame(); original=streak_continuation_details(f)
    f.loc[3,['close_1515','recent_high_10d','recent_high_20d']]=9999.
    pd.testing.assert_frame_equal(original.iloc[:3],streak_continuation_details(f).iloc[:3])


def test_calendar_context_detects_missing_feature_sessions():
    from technical_analysis_service.analysis.cascade.streak_continuation import add_streak_history_context
    f=frame(); f['signal_date']=pd.to_datetime(['2026-01-02','2026-01-06','2026-01-07','2026-01-08'])
    calendar=pd.DataFrame({'calendar_date':pd.to_datetime(['2026-01-02','2026-01-05','2026-01-06','2026-01-07','2026-01-08']),
                           'is_trading_day':[True]*5})
    contextual=add_streak_history_context(f,calendar)
    assert contextual.history_break.tolist()==[False,True,False,False]
    result=streak_continuation_details(contextual)
    assert result.streak.tolist()==[1,1,2,3]
    assert pd.isna(result.prediction.iloc[2])
    assert result.prediction.iloc[3]=='CALL'


def test_ungraded_gap_does_not_count_as_a_recall_miss():
    from technical_analysis_service.diagnostics.strategy_miss_audit import classify_outcomes
    f=frame(); f.loc[2,'gap_open_atr']=None
    detail=streak_continuation_details(f)
    labels=pd.Series(['CALL']*4).where(detail.evaluation_eligible)
    scored=classify_outcomes(labels,detail.prediction,'CALL')
    assert not scored.graded.iloc[2] and not scored.recall_miss.iloc[2]


def test_conflict_overlay_preserves_original_series_and_only_suppresses_confirmed_calls():
    call=pd.Series(['CALL','NO_POSITION',pd.NA]); put=pd.Series(['PUT']*3)
    out=research_conflict_signals(call,put)
    assert out.exhaustion_put_research_overlay.tolist()==['NO_POSITION','PUT','PUT']
    assert put.tolist()==['PUT']*3
    assert get_strategy_registry().get_meta('StreakContinuationCall').strategy_type=='SIGNAL'
    assert get_strategy_registry().get_meta('StreakContinuationCall').can_hard_trade


def test_trade_replay_requires_contiguous_bars_and_does_not_invent_touch_order():
    from technical_analysis_service.diagnostics.streak_continuation_backtest import simulate_long_trade
    bars=pd.DataFrame(dict(candle_time=pd.date_range('2026-01-02 09:15', periods=2,freq='5min'),
        open_price=[100.,100.], high_price=[100.2,100.7], low_price=[99.9,99.5], close_price=[100.,100.]))
    result=simulate_long_trade(bars,'2026-01-02',100.)
    assert result['execution_status']=='AMBIGUOUS_BOTH_IN_BAR'
    assert result['underlying_return_lower']==-.004
    assert result['underlying_return_upper']==.006
    assert result['sized_return_lower']==-.002
    assert simulate_long_trade(bars.iloc[1:],'2026-01-02',100.)['execution_status']=='INCOMPLETE_INTRADAY'


def test_trade_replay_exits_at_1510_open_not_later_candle_high():
    from technical_analysis_service.diagnostics.streak_continuation_backtest import simulate_long_trade
    times=pd.date_range('2026-01-02 09:15','2026-01-02 15:10',freq='5min')
    bars=pd.DataFrame(dict(candle_time=times,open_price=100.,high_price=100.2,low_price=99.9,close_price=100.))
    bars.loc[bars.index[-1],['open_price','high_price','close_price']]=[100.1,101.,100.8]
    result=simulate_long_trade(bars,'2026-01-02',100.)
    assert result['execution_status']=='TIME_EXIT'
    assert result['underlying_return_lower']==pytest.approx(.001)
