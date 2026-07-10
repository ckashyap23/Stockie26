from __future__ import annotations

import pytest
import requests

from scripts.daily_NIFTY.daily_NIFTYoption_snapshot import get_spot_quote, kite_quote_with_retries


class _FakeKiteApi:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def quote(self, keys: list[str]) -> dict[str, object]:
        self.calls.append(keys)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _FakeKiteClient:
    def __init__(self, responses: list[object]) -> None:
        self.kite = _FakeKiteApi(responses)


def test_kite_quote_retries_transient_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(
        "scripts.daily_NIFTY.daily_NIFTYoption_snapshot.time_module.sleep",
        sleeps.append,
    )

    client = _FakeKiteClient([
        requests.exceptions.ReadTimeout("read timeout=7"),
        {"NSE:NIFTY 50": {"last_price": 25100.0}},
    ])

    quote = kite_quote_with_retries(
        kite_client=client,
        keys=["NSE:NIFTY 50"],
        context="spot:NSE:NIFTY 50",
        max_retries=2,
        retry_sleep_seconds=0.5,
    )

    assert quote["NSE:NIFTY 50"]["last_price"] == 25100.0
    assert len(client.kite.calls) == 2
    assert sleeps == [0.5]


def test_get_spot_quote_uses_retry_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "scripts.daily_NIFTY.daily_NIFTYoption_snapshot.time_module.sleep",
        lambda _: None,
    )
    client = _FakeKiteClient([
        requests.exceptions.ReadTimeout("Read timed out"),
        {"NSE:NIFTY 50": {"last_price": 25000.5}},
    ])

    assert get_spot_quote(client, "NIFTY", max_retries=1, retry_sleep_seconds=0) == 25000.5
    assert len(client.kite.calls) == 2


def test_kite_quote_does_not_retry_non_transient_error() -> None:
    client = _FakeKiteClient([RuntimeError("invalid token")])

    with pytest.raises(RuntimeError, match="Kite quote failed after 1 attempts"):
        kite_quote_with_retries(
            kite_client=client,
            keys=["NSE:NIFTY 50"],
            context="spot:NSE:NIFTY 50",
            max_retries=2,
            retry_sleep_seconds=0,
        )

    assert len(client.kite.calls) == 1
