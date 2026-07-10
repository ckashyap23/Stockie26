import pytest

from src.execution.position_sizing import size_long_option_position


def test_full_corpus_sizes_whole_option_lots() -> None:
    lots, quantity = size_long_option_position(409.4, 65, 100_000, 1.0)
    assert lots == 3
    assert quantity == 195


def test_position_sizing_rejects_insufficient_allocation() -> None:
    with pytest.raises(ValueError, match="cannot fund one lot"):
        size_long_option_position(409.4, 65, 100_000, 0.10)
