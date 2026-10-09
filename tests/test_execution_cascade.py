from src.execution.cascade import (
    DEFAULT_OPTION_DELTA,
    compute_anchored_cascade_levels,
    compute_cascade_levels,
    compute_delta_adjusted_levels,
)


def test_delta_adjusted_levels_match_underlying_move_policy() -> None:
    levels = compute_delta_adjusted_levels(
        entry_price=387,
        underlying_price=23_000,
        option_delta=0.58,
        target_pct=0.006,
        stop_loss_pct=0.004,
    )

    assert round(levels.target_price, 2) == 467.04
    assert round(levels.stop_loss_price, 2) == 333.64


def test_delta_adjusted_levels_use_default_for_missing_or_invalid_delta() -> None:
    missing = compute_delta_adjusted_levels(100, 20_000, None, 0.006, 0.004)
    invalid = compute_delta_adjusted_levels(100, 20_000, 1.5, 0.006, 0.004)

    assert missing.option_delta == DEFAULT_OPTION_DELTA
    assert invalid.option_delta == DEFAULT_OPTION_DELTA


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


def test_anchored_cascade_reuses_initial_delta_adjusted_distances() -> None:
    levels = compute_anchored_cascade_levels(
        base_price=467.04,
        initial_target_move=80.04,
        initial_stop_move=53.36,
        completed_targets=1,
        sl_divider=10,
        n_cap=5,
    )

    assert round(levels.target_price, 2) == 547.08
    assert round(levels.adjusted_stop_move, 2) == 58.70
    assert round(levels.stop_loss_price, 2) == 408.34


def test_anchored_cascade_caps_stop_widening() -> None:
    levels = compute_anchored_cascade_levels(200, 20, 10, 99, 10, 5)

    assert levels.effective_n == 5
    assert levels.target_price == 220
    assert levels.stop_loss_price == 185
