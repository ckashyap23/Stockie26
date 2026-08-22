from datetime import date, datetime, timezone

import pandas as pd

from src.data_manager.global_index_loader import normalize_yfinance_frame, write_global_index_ohlc_csv


INDEX_META = {
    "index_code": "NIKKEI225",
    "index_name": "Nikkei 225",
    "yahoo_symbol": "^N225",
    "region": "Japan",
    "currency": "JPY",
}


def test_normalize_yfinance_frame_flat_columns():
    frame = pd.DataFrame(
        [{"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5, "Adj Close": 1.4, "Volume": 1000}],
        index=pd.to_datetime(["2026-06-24"]),
    )

    rows = normalize_yfinance_frame(frame, INDEX_META, fetched_at=datetime(2026, 6, 25, tzinfo=timezone.utc))

    assert rows == [
        {
            "index_code": "NIKKEI225",
            "index_name": "Nikkei 225",
            "yahoo_symbol": "^N225",
            "region": "Japan",
            "currency": "JPY",
            "trade_date": date(2026, 6, 24),
            "open_price": 1.0,
            "high_price": 2.0,
            "low_price": 0.5,
            "close_price": 1.5,
            "adj_close": 1.4,
            "volume": 1000,
                "source": "yfinance_1d",
                "is_final": True,
            "fetched_at": datetime(2026, 6, 25, tzinfo=timezone.utc),
        }
    ]


def test_normalize_yfinance_frame_multiindex_columns():
    frame = pd.DataFrame(
        [[1.0, 2.0, 0.5, 1.5, 1.4, 1000]],
        index=pd.to_datetime(["2026-06-24"]),
        columns=pd.MultiIndex.from_product(
            [["Open", "High", "Low", "Close", "Adj Close", "Volume"], ["^N225"]]
        ),
    )

    rows = normalize_yfinance_frame(frame, INDEX_META)

    assert len(rows) == 1
    assert rows[0]["index_code"] == "NIKKEI225"
    assert rows[0]["close_price"] == 1.5


def test_write_global_index_ohlc_csv(tmp_path):
    output = write_global_index_ohlc_csv(
        [
            {
                "index_code": "NIKKEI225",
                "trade_date": date(2026, 6, 24),
                "close_price": 1.5,
            }
        ],
        date(2026, 6, 25),
        output_dir=tmp_path,
    )

    assert output == tmp_path / "25-06-2026" / "global_index_ohlc.csv"
    assert output.exists()
