from __future__ import annotations

from datetime import date

import pandas as pd

from scripts.backfill_NIFTY.backfill_nifty_oi_regime import extract_near_month_futures_oi


def test_extract_near_month_futures_oi_legacy_schema() -> None:
    df = pd.DataFrame(
        {
            "SYMBOL": ["NIFTY", "NIFTY", "NIFTY"],
            "INSTRUMENT": ["FUTIDX", "FUTIDX", "OPTIDX"],
            "EXPIRY_DT": ["2026-09-24", "2026-10-29", "2026-09-24"],
            "OPEN_INT": [123456, 222222, 999999],
        }
    )
    oi = extract_near_month_futures_oi(df, date(2026, 9, 4))
    assert oi == 123456


def test_extract_near_month_futures_oi_udiff_schema() -> None:
    df = pd.DataFrame(
        {
            "TckrSymb": ["NIFTY", "NIFTY", "NIFTY"],
            "FinInstrmTp": ["IDF", "IDF", "IDO"],
            "XpryDt": ["2026-09-24", "2026-10-29", "2026-09-24"],
            "OpnIntrst": [555000, 777000, 999000],
        }
    )
    oi = extract_near_month_futures_oi(df, date(2026, 9, 4))
    assert oi == 555000
