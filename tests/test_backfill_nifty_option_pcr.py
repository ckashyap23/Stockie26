from __future__ import annotations

import pandas as pd

from scripts.backfill_NIFTY.backfill_nifty_option_pcr import extract_nifty_option_totals


def test_extract_nifty_option_totals_computes_pcr_from_aggregated_rows() -> None:
    df = pd.DataFrame(
        {
            "FinInstrmTp": ["IDO", "IDO", "IDO", "IDO", "IDF"],
            "TckrSymb": ["NIFTY", "NIFTY", "NIFTY", "NIFTY", "NIFTY"],
            "OptnTp": ["CE", "CE", "PE", "PE", None],
            "OpnIntrst": [1000, 500, 1200, 300, 99999],
            "TtlTradgVol": [200, 100, 240, 60, 99999],
        }
    )

    totals = extract_nifty_option_totals(df)

    assert totals is not None
    assert totals["call_oi"] == 1500.0
    assert totals["put_oi"] == 1500.0
    assert totals["call_vol"] == 300.0
    assert totals["put_vol"] == 300.0
    assert totals["pcr_oi"] == 1.0
    assert totals["pcr_volume"] == 1.0
