"""The cascade engine: scoring, regime-aware precision-floor voting, and the
walk-forward harness. Strategy-roster agnostic — `gather_regime_signals` takes the
roster (regime_families) as a parameter, so the experiment can pass the full roster
and production the promoted subset while sharing this exact engine.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.technical_analysis.strategy_families import get_strategy_family_registry

from .constants import (
    CALL, PUT, FLAT,
    REGIME_STRESS, REGIME_CALM, REGIME_PRECISION_FLOOR,
    MIN_FIRES, WF_WINDOW, WF_MIN_FIRES,
)
from .dataset import _call_ok, _put_ok


# ───────────────────────── metrics ─────────────────────────

@dataclass
class Metrics:
    name: str
    n_call: int
    n_put: int
    precision: float
    recall: float
    f1: float
    call_precision: float
    put_precision: float
    coverage: float


def score_signal(df: pd.DataFrame, signal: pd.Series, name: str) -> Metrics:
    call_ok, put_ok = _call_ok(df), _put_ok(df)
    fired_call = signal == CALL
    fired_put = signal == PUT

    correct_call = int((fired_call & call_ok).sum())
    correct_put = int((fired_put & put_ok).sum())
    n_call, n_put = int(fired_call.sum()), int(fired_put.sum())
    n_fired = n_call + n_put
    correct = correct_call + correct_put

    # opportunities: CALL-eligible days for CALL signals, PUT-eligible for PUT,
    # union for two-sided strategies.
    opp = 0
    if n_call:
        opp += int(call_ok.sum())
    if n_put:
        opp += int(put_ok.sum())
    if n_call and n_put:  # two-sided: opportunity is any day a move happened
        opp = int((call_ok | put_ok).sum())

    precision = correct / n_fired if n_fired else float("nan")
    recall = correct / opp if opp else float("nan")
    f1 = (2 * precision * recall / (precision + recall)
          if n_fired and opp and (precision + recall) > 0 else float("nan"))
    return Metrics(
        name=name, n_call=n_call, n_put=n_put,
        precision=precision, recall=recall, f1=f1,
        call_precision=correct_call / n_call if n_call else float("nan"),
        put_precision=correct_put / n_put if n_put else float("nan"),
        coverage=n_fired / len(df),
    )


def _fmt(x: float) -> str:
    return "  n/a" if x != x else f"{x:.3f}"


# ───────────────────────── final daily prediction (cascade) ─────────────────────────

def gather_regime_signals(df: pd.DataFrame,
                          regime_families: dict[str, dict]) -> dict[str, dict[str, pd.Series]]:
    """{regime: {variant_name: signal Series}} built from each regime's families.
    Signals are computed on the full frame; they are sliced/scored per regime.

    `regime_families` is the roster to evaluate ({regime: {family_name: fn}}); the
    experiment passes the full roster and production the promoted subset."""
    out: dict[str, dict[str, pd.Series]] = {}
    for regime, families in regime_families.items():
        sigs: dict[str, pd.Series] = {}
        for fn in families.values():
            for col, sig in fn(df).items():
                name = col.replace("strategy_", "").replace("_signal", "")
                sigs[name] = sig
        out[regime] = sigs
    return out


def _side_precisions(elig_df: pd.DataFrame, signals: dict[str, pd.Series]):
    """Per-variant (call_precision, n_call, put_precision, n_put) measured on elig_df."""
    call_ok, put_ok = _call_ok(elig_df), _put_ok(elig_df)
    out = {}
    for name, sig in signals.items():
        s = sig.loc[elig_df.index]
        fc, fp = s == CALL, s == PUT
        nc, npp = int(fc.sum()), int(fp.sum())
        cp = int((fc & call_ok).sum()) / nc if nc else float("nan")
        pp = int((fp & put_ok).sum()) / npp if npp else float("nan")
        out[name] = (cp, nc, pp, npp)
    return out


def _regime_eligibility(regime: str, signals: dict[str, pd.Series],
                        elig_df: pd.DataFrame):
    """Eligible CALL/PUT voters for one regime: side precision (on elig_df) clears
    that regime's floor with >= MIN_FIRES fires."""
    floor = REGIME_PRECISION_FLOOR[regime]
    prec = _side_precisions(elig_df, signals)
    call_elig = {n: cp for n, (cp, nc, pp, npp) in prec.items()
                 if nc >= MIN_FIRES and cp > floor}
    put_elig = {n: pp for n, (cp, nc, pp, npp) in prec.items()
                if npp >= MIN_FIRES and pp > floor}
    return call_elig, put_elig


def _pick(idx, signals, call_elig, put_elig) -> str:
    """Highest-precision eligible CALL vs PUT vote for one day; higher wins."""
    registry = get_strategy_family_registry()

    def best_by_family(eligible, direction):
        representatives: dict[str, float] = {}
        for name, precision in eligible.items():
            if signals[name].loc[idx] != direction:
                continue
            meta = registry.get_meta(name)
            if not meta.can_hard_trade:
                continue
            representatives[meta.family] = max(
                representatives.get(meta.family, float("-inf")), precision
            )
        return max(representatives.values(), default=None)

    best_call = best_by_family(call_elig, CALL)
    best_put = best_by_family(put_elig, PUT)
    if best_call is not None and (best_put is None or best_call > best_put):
        return CALL
    if best_put is not None and (best_call is None or best_put > best_call):
        return PUT
    return FLAT


def build_regime_cascade(df: pd.DataFrame,
                         regime_signals: dict[str, dict[str, pd.Series]],
                         elig_frames: dict[str, pd.DataFrame]):
    """One prediction per day using only the day's regime voters. Eligibility for
    each regime is fit on elig_frames[regime] (that regime's slice, labelled at the
    regime threshold). Returns (final_position Series, {regime: (call_elig, put_elig)})."""
    elig = {regime: _regime_eligibility(regime, sigs, elig_frames[regime])
            for regime, sigs in regime_signals.items()}
    regimes = df["regime"]
    pred = pd.Series(FLAT, index=df.index)
    for idx in df.index:
        regime = regimes.loc[idx]
        call_elig, put_elig = elig[regime]
        pred.loc[idx] = _pick(idx, regime_signals[regime], call_elig, put_elig)
    return pred, elig


def walk_forward_regime(df: pd.DataFrame,
                        regime_signals: dict[str, dict[str, pd.Series]],
                        window: int = WF_WINDOW):
    """Rolling out-of-sample regime cascade. For each day i (after a `window`
    warm-up), eligibility is fit only on the trailing `window` days that share day
    i's regime, then day i is predicted. Nothing from day i onward leaks in."""
    regimes = df["regime"]
    call_ok_all, put_ok_all = _call_ok(df), _put_ok(df)
    pred = pd.Series(FLAT, index=df.index)

    for pos in range(window, len(df)):
        idx = df.index[pos]
        regime = regimes.loc[idx]
        floor = REGIME_PRECISION_FLOOR[regime]
        sigs = regime_signals[regime]
        win = df.iloc[pos - window:pos]
        win_same = win[win["regime"] == regime]
        if len(win_same) < WF_MIN_FIRES:
            continue
        cok, pok = call_ok_all.loc[win_same.index], put_ok_all.loc[win_same.index]

        call_elig, put_elig = {}, {}
        for name, sig in sigs.items():
            w = sig.loc[win_same.index]
            fc, fp = w == CALL, w == PUT
            nc, npp = int(fc.sum()), int(fp.sum())
            if nc >= WF_MIN_FIRES:
                cp = int((fc & cok).sum()) / nc
                if cp > floor:
                    call_elig[name] = cp
            if npp >= WF_MIN_FIRES:
                pp = int((fp & pok).sum()) / npp
                if pp > floor:
                    put_elig[name] = pp

        pred.loc[idx] = _pick(idx, sigs, call_elig, put_elig)

    return pred


def score_final(df_sub: pd.DataFrame, pred: pd.Series) -> dict:
    """Grade a final one-per-day prediction against actual_trade_label."""
    call_ok, put_ok = _call_ok(df_sub), _put_ok(df_sub)
    label = df_sub["actual_trade_label"]
    move = call_ok | put_ok
    fc, fp, ff = pred == CALL, pred == PUT, pred == FLAT
    fired = fc | fp
    correct = (fc & call_ok) | (fp & put_ok)
    n_fired = int(fired.sum())
    n_move = int(move.sum())
    # wrong-way = took a side but the OPPOSITE exclusive move happened (the costly error)
    wrong_way = int(((fc & (label == PUT)) | (fp & (label == CALL))).sum())
    correct_flat = int((ff & (label == FLAT)).sum())
    put_base = float(put_ok.mean())
    dir_prec = int(correct.sum()) / n_fired if n_fired else float("nan")
    return {
        "n": len(df_sub),
        "n_move": n_move,
        "n_call": int(fc.sum()), "n_put": int(fp.sum()), "n_flat": int(ff.sum()),
        "dir_precision": dir_prec,
        "dir_recall": int(correct.sum()) / n_move if n_move else float("nan"),
        "wrong_way_rate": wrong_way / n_fired if n_fired else float("nan"),
        "overall_accuracy": (int(correct.sum()) + correct_flat) / len(df_sub),
        "put_base": put_base,
        "lift": dir_prec / put_base if put_base else float("nan"),
    }


def _confusion_lines(df_sub: pd.DataFrame, pred: pd.Series) -> list[str]:
    label = df_sub["actual_trade_label"]
    acts = [CALL, PUT, "BOTH", FLAT]
    disp = {CALL: "CALL", PUT: "PUT", "BOTH": "BOTH", FLAT: "NONE"}
    lines = [f"    {'pred \\ actual':<16}" + "".join(f"{disp[a]:>7}" for a in acts)]
    for p in [CALL, PUT, FLAT]:
        row = pred == p
        cells = "".join(f"{int((row & (label == a)).sum()):>7}" for a in acts)
        lines.append(f"    {disp[p]:<16}{cells}")
    return lines
