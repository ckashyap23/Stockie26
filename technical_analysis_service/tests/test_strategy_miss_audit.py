import pandas as pd

from technical_analysis_service.diagnostics.strategy_miss_audit import (
    calendar_alignment, classify_outcomes, daily_price_context, traced_production_rules,
)


def test_misses_respect_direction_both_and_pending_labels():
    labels=pd.Series(['CALL','PUT','BOTH','NO_POSITION',None])
    predictions=pd.Series(['CALL','CALL','NO_POSITION','CALL','PUT'])
    result=classify_outcomes(labels,predictions,'CALL')
    assert result['correct_fire'].tolist()==[True,False,False,False,False]
    assert result['precision_miss'].tolist()==[False,True,False,True,False]
    assert result['recall_miss'].tolist()==[False,False,True,False,False]
    assert result['graded'].tolist()==[True,True,True,True,False]
    assert result['fired'].iloc[-1]
    two_sided=classify_outcomes(labels,predictions,'TWO_SIDED')
    assert two_sided['recall_miss'].iloc[1]
    both=classify_outcomes(pd.Series(['BOTH','BOTH']),pd.Series(['CALL','PUT']),'TWO_SIDED')
    assert both['correct_fire'].all() and not both['precision_miss'].any()


def test_actual_predicate_trace_preserves_every_production_rule():
    base=pd.DataFrame({
        'close_1515':[100.,105.,90.], 'open_915':[101.,100.,96.],
        'ma20':[101.,100.,95.], 'ma50':[99.,100.,94.], 'ma20_slope':[.01,.02,-.02],
        'rsi5':[25.,90.,40.], 'ret_3d':[-.02,.04,-.01], 'ret_5d':[-.01,.06,-.01],
        'atr14':[2.,2.,2.], 'vix_close':[15.,16.,17.], 'oi_regime':['short_buildup','long_buildup','short_buildup'],
        'support_distance_10d':[.1,3.,1.], 'gap_open_atr':[-.3,.1,.1],
        'nifty_gap_pct':[-.01,.01,.001], 'pcr_oi':[1.,1.,1.],
        'range_position_20d':[.1,.95,.4], 'price_zscore_20d':[0.,2.,-1.],
        'high_day':[102.,107.,95.], 'low_day':[99.,102.,88.], 'close_1415':[101.,106.,91.],
    })
    signals,conditions=traced_production_rules(base)
    assert len(conditions)==6
    for name,terms in conditions.items():
        reconstructed=pd.Series(True,index=base.index)
        for _,passed,_ in terms:
            reconstructed &= passed.fillna(False)
        pd.testing.assert_series_equal(reconstructed,signals[name].isin(['CALL','PUT']),check_names=False)
    assert any('trend_pullback_rsi_ok' in term[0] for term in conditions['strategy_TrendPullbackCall_signal'])


def test_price_context_separates_gap_and_intraday_movement():
    base=pd.DataFrame({'open_915':[100.,120.],'close_1515':[110.,130.],
                       'next_open':[120.,None],'next_close':[130.,None],
                       'future_high_nd':[135.,None],'future_low_nd':[118.,None]})
    result=daily_price_context(base)
    assert round(result.loc[0,'signal_open_to_close_pct'],6)==10.
    assert round(result.loc[0,'execution_gap_pct'],6)==9.090909
    assert round(result.loc[0,'execution_open_to_close_pct'],6)==8.333333
    assert round(result.loc[1,'signal_close_to_close_pct'],6)==18.181818
    assert pd.isna(result.loc[1,'execution_open_to_close_pct'])


def test_scoring_excludes_labels_shifted_across_missing_calendar_sessions():
    base=pd.DataFrame({'signal_date':['2026-05-18','2026-05-20','2026-05-21']})
    valid=calendar_alignment(base,['2026-05-18','2026-05-19','2026-05-20','2026-05-21','2026-05-22'],1)
    assert valid.tolist()==[False,True,False]
    assert not calendar_alignment(base,['2026-05-18','2026-05-19','2026-05-20','2026-05-21','2026-05-22'],2).any()
