"""
Full production strategy audit — identify pruning candidates.
Same logic as Flask build_promoted_roster_table() + strategy_type from registry.
"""
import pandas as pd
import numpy as np

from src.technical_analysis.cascade.dataset import build_base, regime_frame, _call_ok, _put_ok
from src.technical_analysis.cascade.engine import _side_precisions
from src.technical_analysis.cascade.strategies import ALL_PARTICIPATING_REGIME_FAMILIES
from src.technical_analysis.cascade.constants import (
    PRODUCTION_BACKTEST_START, REGIME_STRESS, REGIME_CALM,
    REGIME_PRECISION_FLOOR, MIN_FIRES, CALL, PUT,
)
from src.technical_analysis.prediction.signal_strength import add_raw_direction
from src.technical_analysis.strategy_families import get_strategy_family_registry

print("Loading historical data...")
resolved = add_raw_direction(build_base())
resolved = resolved[
    (pd.to_datetime(resolved["signal_date"]) >= pd.Timestamp(PRODUCTION_BACKTEST_START))
    & resolved["next_open"].notna()
].reset_index(drop=True)
print(f"Rows: {len(resolved)}  ({resolved['signal_date'].min()} → {resolved['signal_date'].max()})")

registry = get_strategy_family_registry()

rows = []
for regime in [REGIME_STRESS, REGIME_CALM]:
    floor = REGIME_PRECISION_FLOOR[regime]
    families = ALL_PARTICIPATING_REGIME_FAMILIES[regime]
    elig_df = regime_frame(resolved, regime)
    n_call_opps = int(_call_ok(elig_df).sum())
    n_put_opps  = int(_put_ok(elig_df).sum())

    signals: dict = {}
    for fn in families.values():
        for col, sig in fn(resolved).items():
            name = col.replace("strategy_", "").replace("_signal", "")
            signals[name] = sig

    prec = _side_precisions(elig_df, signals)
    for name, (cp, nc, pp, npp) in sorted(prec.items()):
        try:
            meta = registry.get_meta(name)
            stype  = meta.strategy_type
            family = meta.family
            direction = meta.direction
        except KeyError:
            stype = family = direction = "UNKNOWN"

        # CALL side
        if nc > 0:
            correct_c   = round(cp * nc) if cp == cp else 0
            call_recall = correct_c / n_call_opps if n_call_opps else float("nan")
            call_f1     = (2 * cp * call_recall / (cp + call_recall)
                           if cp == cp and call_recall == call_recall and (cp + call_recall) > 0
                           else float("nan"))
            floor_pass  = nc >= MIN_FIRES and cp == cp and cp > floor
            rows.append({
                "regime": regime, "family": family, "strategy": name,
                "type": stype, "side": "CALL",
                "fires": nc, "precision": round(cp, 3) if cp == cp else None,
                "recall": round(call_recall, 3) if call_recall == call_recall else None,
                "F1": round(call_f1, 3) if call_f1 == call_f1 else None,
                "floor": floor, "floor_pass": floor_pass,
            })
        # PUT side
        if npp > 0:
            correct_p  = round(pp * npp) if pp == pp else 0
            put_recall = correct_p / n_put_opps if n_put_opps else float("nan")
            put_f1     = (2 * pp * put_recall / (pp + put_recall)
                          if pp == pp and put_recall == put_recall and (pp + put_recall) > 0
                          else float("nan"))
            floor_pass = npp >= MIN_FIRES and pp == pp and pp > floor
            rows.append({
                "regime": regime, "family": family, "strategy": name,
                "type": stype, "side": "PUT",
                "fires": npp, "precision": round(pp, 3) if pp == pp else None,
                "recall": round(put_recall, 3) if put_recall == put_recall else None,
                "F1": round(put_f1, 3) if put_f1 == put_f1 else None,
                "floor": floor, "floor_pass": floor_pass,
            })

df = pd.DataFrame(rows)

# ── full table sorted by F1 desc ─────────────────────────────────────────────
df_sorted = df.sort_values(["regime", "type", "F1"], ascending=[True, True, False])

pd.set_option("display.max_rows", 200)
pd.set_option("display.max_colwidth", 45)
pd.set_option("display.width", 160)

print("\n" + "="*140)
print("FULL PRODUCTION STRATEGY AUDIT")
print("="*140)
print(df_sorted[[
    "regime", "family", "strategy", "type", "side",
    "fires", "precision", "recall", "F1", "floor", "floor_pass"
]].to_string(index=False))

# ── pruning candidates ─────────────────────────────────────────────────────────
print("\n" + "="*140)
print("PRUNING CANDIDATES  (precision < floor  OR  fires < MIN_FIRES = 5)")
print(f"Floors: STRESS={REGIME_PRECISION_FLOOR[REGIME_STRESS]}, CALM={REGIME_PRECISION_FLOOR[REGIME_CALM]}")
print("="*140)

candidates = df[
    (~df["floor_pass"]) & (df["fires"] >= MIN_FIRES)   # enough fires but below floor
].sort_values(["type", "regime", "precision"])
print(candidates[[
    "regime", "family", "strategy", "type", "side",
    "fires", "precision", "floor", "F1"
]].to_string(index=False))

# ── low-fire strategies (never really tested) ──────────────────────────────────
print("\n" + "="*140)
print(f"LOW-FIRE STRATEGIES  (fires < {MIN_FIRES}) — insufficient data to evaluate")
print("="*140)
low_fire = df[df["fires"] < MIN_FIRES].sort_values(["type", "regime"])
print(low_fire[[
    "regime", "family", "strategy", "type", "side", "fires", "precision"
]].to_string(index=False))

# ── passing strategies ─────────────────────────────────────────────────────────
print("\n" + "="*140)
print("PASSING STRATEGIES (floor_pass = True)")
print("="*140)
passing = df[df["floor_pass"]].sort_values("F1", ascending=False)
print(passing[[
    "regime", "family", "strategy", "type", "side",
    "fires", "precision", "recall", "F1"
]].to_string(index=False))
