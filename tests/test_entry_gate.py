from src.execution.entry_gate import evaluate_promoted_call_entry


def test_promoted_call_waits_after_negative_gap():
    decision = evaluate_promoted_call_entry(
        final_prediction="NO_POSITION", promoted_prediction="CALL",
        signal_day_close_1515=25000, d1_open=24900, current_spot=25010,
    )
    assert not decision.allow_entry
    assert decision.entry_action == "WAIT_FOR_CALL_RECLAIM"
    assert decision.reclaim_level == 25025


def test_promoted_call_enters_after_reclaim():
    decision = evaluate_promoted_call_entry(
        final_prediction="NO_POSITION", promoted_prediction="CALL",
        signal_day_close_1515=25000, d1_open=24900, current_spot=25030,
    )
    assert decision.allow_entry
    assert decision.entry_action == "ENTER_CALL_RECLAIMED"


def test_base_call_is_not_subject_to_reclaim_gate():
    decision = evaluate_promoted_call_entry(
        final_prediction="CALL", promoted_prediction="NO_POSITION",
        signal_day_close_1515=25000, d1_open=24900, current_spot=24900,
    )
    assert decision.allow_entry
