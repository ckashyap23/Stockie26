"""Compare baseline, family-only, and family-plus-watch-candidate predictions."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src.technical_analysis.cascade.constants import PRODUCTION_BACKTEST_START
from src.technical_analysis.cascade.dataset import build_base, regime_frame
from src.technical_analysis.cascade.engine import (
    build_regime_cascade,
    gather_regime_signals,
    score_final,
    walk_forward_regime,
)
from src.technical_analysis.cascade.constants import WF_WINDOW
from src.technical_analysis.cascade.strategies import (
    PROMOTED_REGIME_FAMILIES,
    WATCH_ONLY_REGIME_FAMILIES,
)
from src.technical_analysis.cascade.watch_promotion import add_watch_promotions


def _effective(df, base, signals, same_family, precisions, enforce_authority=True):
    promoted = add_watch_promotions(
        df,
        base,
        signals,
        strategy_precisions=precisions,
        require_same_family_confirmation=same_family,
        enforce_strategy_policy=enforce_authority,
    )
    effective = base.where(base != "NO_POSITION", promoted["promoted_prediction"])
    return effective, promoted


def main() -> None:
    base = build_base().reset_index(drop=True)
    resolved = base[
        (pd.to_datetime(base["signal_date"]) >= pd.Timestamp(PRODUCTION_BACKTEST_START))
        & base["next_open"].notna()
    ].reset_index(drop=True)
    hard_signals = gather_regime_signals(resolved, PROMOTED_REGIME_FAMILIES)
    watch_only = gather_regime_signals(resolved, WATCH_ONLY_REGIME_FAMILIES)
    all_watch = {
        regime: {**hard_signals.get(regime, {}), **watch_only.get(regime, {})}
        for regime in PROMOTED_REGIME_FAMILIES
    }
    elig_frames = {r: regime_frame(resolved, r) for r in PROMOTED_REGIME_FAMILIES}
    hard, elig = build_regime_cascade(resolved, hard_signals, elig_frames)
    precisions = {
        regime: {**call_elig, **put_elig}
        for regime, (call_elig, put_elig) in elig.items()
    }

    versions = {
        "A_pre_family": _effective(resolved, hard, hard_signals, False, precisions, False),
        "B_family_only": _effective(resolved, hard, hard_signals, True, precisions),
        "C_family_plus_new_watch": _effective(resolved, hard, all_watch, True, precisions),
    }
    rows = []
    family_rows = []
    for name, (prediction, audit) in versions.items():
        metrics = score_final(resolved, prediction)
        watch_count = int(audit["watch_signal"].notna().sum())
        promotion_count = int(prediction.ne(hard).sum())
        actual = resolved["actual_trade_label"]
        setup_direction = audit["watch_signal"].map({
            "CALL_3D_WATCH": "CALL", "PUT_3D_WATCH": "PUT",
        })
        setup_correct = (
            (prediction.eq("CALL") | setup_direction.eq("CALL")) & actual.isin(["CALL", "BOTH"])
        ) | (
            (prediction.eq("PUT") | setup_direction.eq("PUT")) & actual.isin(["PUT", "BOTH"])
        )
        move_days = actual.isin(["CALL", "PUT", "BOTH"])
        rows.append({
            "version": name,
            "precision": metrics["dir_precision"],
            "recall": metrics["dir_recall"],
            "hard_trades": metrics["n_call"] + metrics["n_put"],
            "watches": watch_count,
            "promotions": promotion_count,
            "setup_recall_including_watches": float(setup_correct.sum() / move_days.sum()),
            "blocked_no_same_family": int(
                audit["promotion_block_reason"].eq("NO_SAME_FAMILY_CONFIRMATION").sum()
            ),
        })
        promoted_mask = audit["promoted_prediction"].isin(["CALL", "PUT"])
        wrong_hard = prediction.isin(["CALL", "PUT"]) & ~(
            (prediction.eq("CALL") & actual.isin(["CALL", "BOTH"]))
            | (prediction.eq("PUT") & actual.isin(["PUT", "BOTH"]))
        )
        recall_miss = prediction.eq("NO_POSITION") & actual.isin(["CALL", "PUT"])
        family_values = sorted(set(
            audit.loc[promoted_mask, "confirming_family"].dropna().astype(str)
        ) | set(audit.loc[wrong_hard, "confirming_family"].dropna().astype(str))
          | set(audit.loc[recall_miss, "watch_family"].dropna().astype(str)))
        for family in family_values:
            family_rows.append({
                "version": name,
                "strategy_family": family,
                "promotions": int((promoted_mask & audit["confirming_family"].eq(family)).sum()),
                "precision_misses": int((wrong_hard & audit["confirming_family"].eq(family)).sum()),
                "recall_misses_with_watch": int((recall_miss & audit["watch_family"].eq(family)).sum()),
            })
    output = ROOT / "output/backtest/NIFTY/production/strategy_family_comparison.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    result = pd.DataFrame(rows)
    result.to_csv(output, index=False)
    family_output = output.with_name("strategy_family_diagnostics.csv")
    pd.DataFrame(family_rows).to_csv(family_output, index=False)
    print(result.to_string(index=False))
    print(f"Written to {output}")
    print(f"Family diagnostics written to {family_output}")

    # Honest out-of-sample comparison: build the base cascade with rolling
    # eligibility, then apply each watch policy sequentially only to OOS rows.
    wf_base_all = walk_forward_regime(resolved, hard_signals)
    wf_df = resolved.iloc[WF_WINDOW:]
    wf_base = wf_base_all.loc[wf_df.index]
    wf_hard_signals = {
        regime: {name: signal.loc[wf_df.index] for name, signal in signals.items()}
        for regime, signals in hard_signals.items()
    }
    wf_all_watch = {
        regime: {name: signal.loc[wf_df.index] for name, signal in signals.items()}
        for regime, signals in all_watch.items()
    }
    wf_versions = {
        "A_pre_family": _effective(wf_df, wf_base, wf_hard_signals, False, {}, False),
        "B_family_only": _effective(wf_df, wf_base, wf_hard_signals, True, {}),
        "C_family_plus_new_watch": _effective(wf_df, wf_base, wf_all_watch, True, {}),
    }
    wf_rows = []
    for name, (prediction, audit) in wf_versions.items():
        metrics = score_final(wf_df, prediction)
        wf_rows.append({
            "version": name,
            "precision": metrics["dir_precision"],
            "recall": metrics["dir_recall"],
            "hard_trades": metrics["n_call"] + metrics["n_put"],
            "promotions": int(prediction.ne(wf_base).sum()),
            "blocked_no_same_family": int(
                audit["promotion_block_reason"].eq("NO_SAME_FAMILY_CONFIRMATION").sum()
            ),
        })
    wf_output = output.with_name("strategy_family_walk_forward_comparison.csv")
    pd.DataFrame(wf_rows).to_csv(wf_output, index=False)

    pre_pred, _ = wf_versions["A_pre_family"]
    current_pred, current_audit = wf_versions["C_family_plus_new_watch"]
    changed = current_pred.ne(pre_pred)
    changes = wf_df.loc[changed, ["signal_date", "regime", "actual_trade_label"]].copy()
    changes["pre_family_prediction"] = pre_pred.loc[changed]
    changes["family_prediction"] = current_pred.loc[changed]
    changes["promotion_reason"] = current_audit.loc[changed, "promotion_reason"]
    changes["watch_family"] = current_audit.loc[changed, "watch_family"]
    changes["confirming_family"] = current_audit.loc[changed, "confirming_family"]
    changes_output = output.with_name("strategy_family_walk_forward_changes.csv")
    changes.to_csv(changes_output, index=False)
    print("\nWalk-forward comparison")
    print(pd.DataFrame(wf_rows).to_string(index=False))
    print(f"Walk-forward changes written to {changes_output}")


if __name__ == "__main__":
    main()
