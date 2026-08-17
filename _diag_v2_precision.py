"""
Compute precision/recall for DeclineContinuationPut_ATR vs DeclineContinuationPut_ATR_v2
over all historical dates (mirrors the Production Strategies table logic exactly).
"""
import pandas as pd
import numpy as np

from src.technical_analysis.cascade.dataset import build_base, regime_frame, _call_ok, _put_ok
from src.technical_analysis.cascade.strategies import decline_continuation_put
from src.technical_analysis.cascade.constants import (
    PRODUCTION_BACKTEST_START, REGIME_STRESS, REGIME_CALM, CALL, PUT,
)
from src.technical_analysis.prediction.signal_strength import add_raw_direction

# ── load & label historical data ─────────────────────────────────────────────
print("Loading historical feature data from DB...")
resolved = add_raw_direction(build_base())
resolved = resolved[
    (pd.to_datetime(resolved["signal_date"]) >= pd.Timestamp(PRODUCTION_BACKTEST_START))
    & resolved["next_open"].notna()
].reset_index(drop=True)
print(f"Rows after filter: {len(resolved)}  ({resolved['signal_date'].min()} → {resolved['signal_date'].max()})")

# ── compute strategy signals ──────────────────────────────────────────────────
# decline_continuation_put() returns both v1 and v2 signals
all_signals = decline_continuation_put(resolved)
v1_sig = all_signals["strategy_DeclineContinuationPut_ATR_signal"]
v2_sig = all_signals["strategy_DeclineContinuationPut_ATR_v2_signal"]

print(f"\nSignal value counts (full history):")
print("v1:", v1_sig.value_counts().to_dict())
print("v2:", v2_sig.value_counts().to_dict())

# ── helper: compute metrics for one signal in one regime ─────────────────────
def metrics(sig: pd.Series, name: str, regime: str, elig_df: pd.DataFrame) -> dict:
    put_ok = _put_ok(elig_df)
    call_ok = _call_ok(elig_df)
    n_put_opps = int(put_ok.sum())
    n_call_opps = int(call_ok.sum())

    # Only evaluate the PUT side (this is a PUT-only strategy)
    fired = sig.reindex(elig_df.index) == PUT
    n_fired = int(fired.sum())
    if n_fired == 0:
        return {"regime": regime, "strategy": name, "side": "PUT",
                "fires": 0, "precision": "n/a", "recall": "n/a", "F1": "n/a",
                "put_opps": n_put_opps}

    put_labels = elig_df["actual_trade_label"]
    correct = int((fired & put_ok).sum())

    prec = correct / n_fired
    rec  = correct / n_put_opps if n_put_opps else float("nan")
    f1   = (2 * prec * rec / (prec + rec)) if (prec + rec) > 0 else float("nan")

    return {
        "regime":    regime,
        "strategy":  name,
        "side":      "PUT",
        "fires":     n_fired,
        "correct":   correct,
        "put_opps":  n_put_opps,
        "precision": f"{prec:.3f}",
        "recall":    f"{rec:.3f}",
        "F1":        f"{f1:.3f}",
    }


# ── evaluate per regime ───────────────────────────────────────────────────────
rows = []
for regime in [REGIME_STRESS, REGIME_CALM]:
    elig_df = regime_frame(resolved, regime)
    print(f"\n── Regime: {regime.upper()}  ({len(elig_df)} rows) ──")
    put_opps = int(_put_ok(elig_df).sum())
    print(f"   PUT opportunities: {put_opps}")

    for sig, name in [(v1_sig, "DeclineContinuationPut_ATR"),
                      (v2_sig, "DeclineContinuationPut_ATR_v2")]:
        r = metrics(sig, name, regime, elig_df)
        rows.append(r)
        print(f"  {name:40s}  fires={r['fires']:4d}  precision={r['precision']}  recall={r['recall']}  F1={r['F1']}")

# ── final summary table ───────────────────────────────────────────────────────
print("\n" + "="*90)
print("SUMMARY — DeclineContinuationPut_ATR vs v2  (PUT side only, both regimes)")
print("="*90)
df = pd.DataFrame(rows)
print(df[["regime","strategy","fires","correct","put_opps","precision","recall","F1"]].to_string(index=False))

# ── show which EXTRA dates v2 fires vs v1 ────────────────────────────────────
print("\n── Overlap analysis (full history) ──")
v1_put = v1_sig == PUT
v2_put = v2_sig == PUT
only_v1 = v1_put & ~v2_put
only_v2 = v2_put & ~v1_put
both    = v1_put & v2_put
print(f"  Both fire:     {int(both.sum())} dates")
print(f"  Only v1 fires: {int(only_v1.sum())} dates  (v1 needed ma5d_slope<0, v2 didn't trigger)")
print(f"  Only v2 fires: {int(only_v2.sum())} dates  (v2 NEW fires — close[t]<close[t-1]<close[t-2])")

# Sample the new v2-only fires
only_v2_dates = resolved.loc[only_v2[only_v2].index, ["signal_date","actual_trade_label","regime","close_1515","ma5d_slope"]].copy()
if not only_v2_dates.empty:
    correct_v2_only = (only_v2_dates["actual_trade_label"].isin(["PUT","BOTH"])).sum()
    print(f"\n  v2-only fires ({len(only_v2_dates)} total, {correct_v2_only} correct = {correct_v2_only/len(only_v2_dates):.1%} precision):")
    print(only_v2_dates.to_string(index=False))
