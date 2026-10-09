from datetime import date

import pandas as pd

from src.technical_analysis.cascade.global_index_features import (
    build_gap_gate_signal,
    build_global_index_features,
    build_global_index_features_cumulative,
)


def test_build_global_index_features_uses_open_to_close_for_lagged_us_and_europe():
    rows = [
        {"index_code": "SP500", "trade_date": date(2026, 6, 23), "open_price": 99.0, "close_price": 100.0},
        {"index_code": "SP500", "trade_date": date(2026, 6, 24), "open_price": 200.0, "close_price": 210.0},
        {"index_code": "SP500", "trade_date": date(2026, 6, 25), "open_price": 100.0, "close_price": 102.0},
        {"index_code": "DAX", "trade_date": date(2026, 6, 23), "open_price": 99.0, "close_price": 100.0},
        {"index_code": "DAX", "trade_date": date(2026, 6, 24), "open_price": 200.0, "close_price": 210.0},
        {"index_code": "DAX", "trade_date": date(2026, 6, 25), "open_price": 100.0, "close_price": 102.0},
        {"index_code": "NIKKEI225", "trade_date": date(2026, 6, 23), "open_price": 99.0, "close_price": 100.0},
        {"index_code": "NIKKEI225", "trade_date": date(2026, 6, 24), "open_price": 100.0, "close_price": 101.0},
        {"index_code": "NIKKEI225", "trade_date": date(2026, 6, 25), "open_price": 101.0, "close_price": 102.0},
    ]

    features = build_global_index_features(pd.DataFrame(rows))
    row = features[features["trade_date"] == pd.Timestamp("2026-06-25")].iloc[0]

    assert round(row["global_ret_SP500"], 6) == round((210.0 - 200.0) / 200.0, 6)
    assert round(row["global_ret_DAX"], 6) == round((210.0 - 200.0) / 200.0, 6)
    assert round(row["global_ret_NIKKEI225"], 6) == round((102.0 - 101.0) / 101.0, 6)


def test_cumulative_features_use_latest_western_open_to_close_session():
    rows = pd.DataFrame(
        [
            {"index_code": "SP500", "trade_date": date(2026, 6, 23), "open_price": 100.0, "close_price": 101.0},
            {"index_code": "SP500", "trade_date": date(2026, 6, 24), "open_price": 200.0, "close_price": 210.0},
            {"index_code": "NIKKEI225", "trade_date": date(2026, 6, 24), "open_price": 99.0, "close_price": 100.0},
            {"index_code": "NIKKEI225", "trade_date": date(2026, 6, 25), "open_price": 100.0, "close_price": 103.0},
        ]
    )

    features = build_global_index_features_cumulative(
        rows,
        pd.to_datetime(["2026-06-24", "2026-06-25"]),
    )
    row = features[features["trade_date"] == pd.Timestamp("2026-06-24")].iloc[0]

    assert round(row["global_ret_SP500"], 6) == round((210.0 - 200.0) / 200.0, 6)
    assert round(row["global_ret_NIKKEI225"], 6) == round((103.0 - 100.0) / 100.0, 6)


def test_cumulative_features_keep_asia_partial_separate_from_final_close():
    rows = pd.DataFrame(
        [
            {
                "index_code": "NIKKEI225", "trade_date": date(2026, 6, 24),
                "open_price": 99.0, "close_price": 100.0,
                "source": "yfinance_1d", "is_final": True,
            },
            {
                "index_code": "NIKKEI225", "trade_date": date(2026, 6, 25),
                "open_price": 101.0, "close_price": 110.0,
                "source": "yfinance_1d", "is_final": True,
            },
            {
                "index_code": "NIKKEI225", "trade_date": date(2026, 6, 25),
                "open_price": 101.0, "close_price": 102.0,
                "source": "yfinance_5m", "is_final": False,
            },
        ]
    )

    features = build_global_index_features_cumulative(
        rows,
        pd.to_datetime(["2026-06-24", "2026-06-25"]),
    )
    row = features[features["trade_date"] == pd.Timestamp("2026-06-24")].iloc[0]

    assert round(row["global_asia_partial_return_mean"], 6) == round((102.0 - 101.0) / 101.0, 6)
    assert round(row["global_asia_overnight_return_mean"], 6) == round((101.0 - 100.0) / 100.0, 6)
    assert round(row["global_ret_NIKKEI225"], 6) == round((102.0 - 100.0) / 100.0, 6)


def test_cumulative_features_reject_stale_asia_final_anchor():
    rows = pd.DataFrame(
        [
            {
                "index_code": "KOSPI",
                "trade_date": date(2024, 7, 24),
                "open_price": 2790.0,
                "close_price": 2800.0,
                "source": "yfinance_1d",
                "is_final": True,
            },
            {
                "index_code": "KOSPI",
                "trade_date": date(2024, 8, 5),
                "open_price": 2700.0,
                "close_price": 2710.0,
                "source": "yfinance_1h_partial",
                "is_final": False,
            },
        ]
    )

    features = build_global_index_features_cumulative(
        rows,
        pd.to_datetime(["2024-08-02", "2024-08-05"]),
    )
    row = features[features["trade_date"] == pd.Timestamp("2024-08-02")].iloc[0]

    assert pd.isna(row["global_ret_KOSPI"])
    assert pd.isna(row["global_asia_overnight_return_mean"])


def test_cumulative_features_do_not_repeat_prior_asia_partial():
    rows = pd.DataFrame(
        [
            {
                "index_code": "NIKKEI225",
                "trade_date": date(2026, 6, 24),
                "open_price": 99.0,
                "close_price": 100.0,
                "source": "yfinance_1d",
                "is_final": True,
            },
            {
                "index_code": "NIKKEI225",
                "trade_date": date(2026, 6, 25),
                "open_price": 101.0,
                "close_price": 102.0,
                "source": "yfinance_1h_partial",
                "is_final": False,
            },
        ]
    )

    features = build_global_index_features_cumulative(
        rows,
        pd.to_datetime(["2026-06-24", "2026-06-25", "2026-06-26"]),
    )
    missing_day = features[
        features["trade_date"] == pd.Timestamp("2026-06-25")
    ].iloc[0]

    assert pd.isna(missing_day["global_ret_NIKKEI225"])
    assert pd.isna(missing_day["global_asia_partial_return_mean"])


def test_cumulative_features_prefer_genuine_final_source():
    rows = pd.DataFrame(
        [
            {
                "index_code": "NIKKEI225",
                "trade_date": date(2026, 6, 24),
                "open_price": 99.0,
                "close_price": 100.0,
                "source": "yfinance_1d_approx",
                "is_final": True,
            },
            {
                "index_code": "NIKKEI225",
                "trade_date": date(2026, 6, 24),
                "open_price": 199.0,
                "close_price": 200.0,
                "source": "yfinance_1d",
                "is_final": True,
            },
            {
                "index_code": "NIKKEI225",
                "trade_date": date(2026, 6, 25),
                "open_price": 202.0,
                "close_price": 204.0,
                "source": "yfinance_5m",
                "is_final": False,
            },
        ]
    )

    features = build_global_index_features_cumulative(
        rows,
        pd.to_datetime(["2026-06-24", "2026-06-25"]),
    )
    row = features[features["trade_date"] == pd.Timestamp("2026-06-24")].iloc[0]

    assert row["global_ret_NIKKEI225"] == (204.0 - 200.0) / 200.0
    assert row["global_asia_overnight_return_mean"] == (202.0 - 200.0) / 200.0


def test_gap_gate_uses_latest_western_open_to_close_session():
    rows = pd.DataFrame(
        [
            {"index_code": "SP500", "trade_date": date(2026, 6, 23), "open_price": 100.0, "close_price": 101.0},
            {"index_code": "SP500", "trade_date": date(2026, 6, 24), "open_price": 200.0, "close_price": 190.0},
            {"index_code": "NIKKEI225", "trade_date": date(2026, 6, 23), "open_price": 100.0, "close_price": 100.0},
            {"index_code": "NIKKEI225", "trade_date": date(2026, 6, 24), "open_price": 100.0, "close_price": 102.0},
        ]
    )

    gate = build_gap_gate_signal(rows)

    assert gate["indices"]["SP500"] == round((190.0 - 200.0) / 200.0, 6)
    assert gate["indices"]["NIKKEI225"] == round((102.0 - 100.0) / 100.0, 6)


def test_build_global_index_features_adds_risk_tone_columns():
    rows = []
    for index_code in ["NIKKEI225", "HANG_SENG", "SHANGHAI", "KOSPI", "ASX200"]:
        rows.append({"index_code": index_code, "trade_date": date(2026, 6, 24), "close_price": 100.0})
        rows.append({"index_code": index_code, "trade_date": date(2026, 6, 25), "close_price": 101.0})

    features = build_global_index_features(pd.DataFrame(rows))
    latest = features[features["trade_date"] == pd.Timestamp("2026-06-25")].iloc[0]

    assert latest["global_positive_count"] == 5
    assert latest["global_breadth"] == 1.0
    assert latest["global_risk_on"] == 1
    assert latest["global_risk_off"] == 0
