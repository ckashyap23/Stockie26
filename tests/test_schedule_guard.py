from datetime import date

from scripts.Common.schedule_guard import is_weekend, skip_if_weekend


def test_is_weekend() -> None:
    assert not is_weekend(date(2026, 10, 9))
    assert is_weekend(date(2026, 10, 10))
    assert is_weekend(date(2026, 10, 11))


def test_skip_if_weekend_returns_false_for_weekday(capsys) -> None:
    assert skip_if_weekend("Test job", date(2026, 10, 9)) is False
    assert capsys.readouterr().out == ""


def test_skip_if_weekend_returns_true_for_weekend(capsys) -> None:
    assert skip_if_weekend("Test job", date(2026, 10, 10)) is True
    assert "Test job: skipping scheduled run on weekend" in capsys.readouterr().out
