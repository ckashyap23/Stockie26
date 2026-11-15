import pandas as pd
import pytest

from technical_analysis_service.diagnostics.gap_up_policy_analysis import apply_policies,drawdown


def test_gap_up_removal_preserves_put_veto_and_existing_abstentions():
    prediction=pd.Series(['CALL','PUT','NO_POSITION','CALL'])
    up=pd.Series([True,False,True,False]); down=pd.Series([False,True,False,False])
    affected,policies=apply_policies(prediction,up,down,pd.Series([1.,1.,1.,.5]))
    assert affected.tolist()==[True,False,False,False]
    assert policies['KEEP_VETO'][0].tolist()==['NO_POSITION','NO_POSITION','NO_POSITION','CALL']
    for name in ['REMOVE_GAP_UP','HALF_NORMAL_ON_GAP_UP','CAP_HALF_STANDARD_ON_GAP_UP']:
        assert policies[name][0].tolist()==['CALL','NO_POSITION','NO_POSITION','CALL']


def test_half_normal_and_half_standard_differ_for_streak_without_changing_direction():
    pred=pd.Series(['CALL','CALL','CALL']); up=pd.Series([True,True,False])
    _,policies=apply_policies(pred,up,pd.Series([False]*3),pd.Series([1.,.5,.5]))
    assert policies['HALF_NORMAL_ON_GAP_UP'][1].tolist()==[.5,.25,.5]
    assert policies['CAP_HALF_STANDARD_ON_GAP_UP'][1].tolist()==[.5,.5,.5]
    assert policies['REMOVE_GAP_UP'][1].tolist()==[1.,.5,.5]
    pd.testing.assert_series_equal(policies['HALF_NORMAL_ON_GAP_UP'][0],policies['REMOVE_GAP_UP'][0])


def test_drawdown_includes_initial_loss_from_zero():
    assert drawdown(pd.Series([-.4,.6,-.5]))==pytest.approx(.5)


def test_production_allows_gap_up_calls_and_retains_put_gap_down_veto():
    from unittest.mock import patch
    from technical_analysis_service.analysis.cascade.pipeline import _apply_guard_layer
    frame=pd.DataFrame({'close_1515':[100.,100.,100.],'next_open':[102.,99.,101.]})
    raw=pd.Series(['CALL','PUT','CALL'])
    # Third row is already blocked by the separate event gate.
    with patch('technical_analysis_service.analysis.cascade.pipeline._apply_event_gate',return_value=pd.Series(['CALL','PUT','NO_POSITION'])):
        result=_apply_guard_layer(frame,raw,{})
    assert result.tolist()==['CALL','NO_POSITION','NO_POSITION']
    assert frame.gap_gate_reason.tolist()==['','GAP_DOWN','']
