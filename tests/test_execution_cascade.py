from src.execution.cascade import compute_cascade_levels


def test_cascade_uses_previous_target_as_base_and_widens_stop() -> None:
    initial = compute_cascade_levels(409.4, 0, 0.05, 0.02, 10, 5)
    assert round(initial.target_price, 2) == 429.87
    assert round(initial.stop_loss_price, 2) == 401.21

    after_t1 = compute_cascade_levels(initial.target_price, 1, 0.05, 0.02, 10, 5)
    assert round(after_t1.target_price, 2) == 451.36
    assert round(after_t1.adjusted_sl_pct, 4) == 0.022
    assert round(after_t1.stop_loss_price, 2) == 420.41


def test_cascade_stop_widening_count_is_capped() -> None:
    levels = compute_cascade_levels(100, 99, 0.05, 0.02, 10, 5)
    assert levels.effective_n == 5
    assert levels.adjusted_sl_pct == 0.03
