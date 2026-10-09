"""Prediction scoring, registry-authorized voting, and walk-forward evaluation.

Each strategy is independent; there is no family grouping or family-level vote.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.technical_analysis.strategy_registry import get_strategy_registry

from .constants import CALL, PUT, FLAT, WF_WINDOW, COOLOFF_WINDOW, TARGET_THRESHOLD
from .dataset import _call_ok, _put_ok, _label_at


# metrics

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

    opp = 0
    if n_call:
        opp += int(call_ok.sum())
    if n_put:
        opp += int(put_ok.sum())
    if n_call and n_put:
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


# final daily prediction (cascade)

def gather_signals(df: pd.DataFrame, strategies: dict) -> dict[str, pd.Series]:
    """Return {variant_name: signal Series} for a common strategy roster."""
    sigs: dict[str, pd.Series] = {}
    for fn in strategies.values():
        for col, sig in fn(df).items():
            name = col.replace("strategy_", "").replace("_signal", "")
            sigs[name] = sig
    return sigs


def _side_precisions(elig_df: pd.DataFrame, signals: dict[str, pd.Series]):
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


def _strategy_eligibility(signals: dict[str, pd.Series], elig_df: pd.DataFrame):
    """Production CALL/PUT voters for the common strategy roster."""
    registry = get_strategy_registry()
    prec = _side_precisions(elig_df, signals)
    call_elig: dict[str, float] = {}
    put_elig: dict[str, float] = {}
    for name, (cp, _nc, pp, _npp) in prec.items():
        try:
            meta = registry.get_meta(name)
        except KeyError:
            continue
        if not meta.can_hard_trade:
            continue
        if meta.direction in {CALL, "TWO_SIDED"}:
            call_elig[name] = float(cp) if cp == cp else 0.0
        if meta.direction in {PUT, "TWO_SIDED"}:
            put_elig[name] = float(pp) if pp == pp else 0.0
    return call_elig, put_elig


def _pick(idx, signals, call_elig, put_elig) -> str:
    registry = get_strategy_registry()

    def best_precision(eligible, direction):
        candidates: list[float] = []
        for name, precision in eligible.items():
            if signals[name].loc[idx] != direction:
                continue
            meta = registry.get_meta(name)
            if not meta.can_hard_trade:
                continue
            candidates.append(precision)
        return max(candidates, default=None)

    best_call = best_precision(call_elig, CALL)
    best_put = best_precision(put_elig, PUT)
    if best_call is not None and (best_put is None or best_call > best_put):
        return CALL
    if best_put is not None and (best_call is None or best_put > best_call):
        return PUT
    return FLAT


def build_strategy_vote(
    df: pd.DataFrame,
    signals: dict[str, pd.Series],
    cooled_strategies: dict | None = None,
) -> tuple[pd.Series, dict, dict]:
    """Compatibility wrapper: any single production SIGNAL can predict."""
    registry = get_strategy_registry()
    final_pred = pd.Series(FLAT, index=df.index)
    for idx in df.index:
        cooled = (cooled_strategies or {}).get(idx, frozenset())
        fired: list[str] = []
        for name, sig in signals.items():
            try:
                meta = registry.get_meta(name)
            except KeyError:
                continue
            if meta.strategy_type != "SIGNAL" or name in cooled:
                continue
            direction = sig.loc[idx] if idx in sig.index else FLAT
            if direction not in (CALL, PUT):
                continue
            fired.append(direction)
        if CALL in fired and PUT not in fired:
            final_pred.loc[idx] = CALL
        elif PUT in fired and CALL not in fired:
            final_pred.loc[idx] = PUT

    return final_pred, {}, {}


def compute_cooloff_strategies(
    df: pd.DataFrame,
    signals: dict[str, pd.Series],
    cooloff_window: int | None = None,
) -> dict:
    """Return strategies temporarily suspended after repeated resolved misses."""
    if cooloff_window is None:
        cooloff_window = COOLOFF_WINDOW

    registry = get_strategy_registry()
    strategy_signals: dict[str, pd.Series] = {}
    for name, sig in signals.items():
        try:
            meta = registry.get_meta(name)
        except KeyError:
            continue
        if meta.strategy_type == "SIGNAL":
            strategy_signals[name] = sig

    if not strategy_signals or "actual_trade_label" not in df.columns:
        return {}

    labels = df["actual_trade_label"].fillna("").tolist()
    n = len(df)
    cooloff: dict = {df.index[pos]: set() for pos in range(n)}

    for name, signal in strategy_signals.items():
        miss_streak = 0
        cooloff_remaining = 0

        for pos in range(n):
            idx = df.index[pos]
            label = labels[pos]
            resolvable = label not in ("", "NO_POSITION", "BOTH", "nan")
            strategy_vote = signal.loc[idx] if idx in signal.index else FLAT

            fired = strategy_vote in (CALL, PUT)
            correct = (
                resolvable and fired and (
                    (strategy_vote == CALL and label in (CALL, "BOTH"))
                    or (strategy_vote == PUT and label in (PUT, "BOTH"))
                )
            )
            wrong = fired and resolvable and not correct

            if cooloff_remaining > 0:
                cooloff[idx].add(name)
                cooloff_remaining -= 1
                if correct:
                    cooloff_remaining = 0
            else:
                if wrong:
                    miss_streak += 1
                    if miss_streak >= 2:
                        cooloff_remaining = cooloff_window
                        miss_streak = 0
                elif correct:
                    miss_streak = 0

    return cooloff


def build_cascade(
    df: pd.DataFrame,
    signals: dict[str, pd.Series],
    elig_frame: pd.DataFrame,
    cooled_strategies: dict | None = None,
):
    """Legacy in-sample cascade kept for summary/tests."""
    call_elig, put_elig = _strategy_eligibility(signals, elig_frame)
    pred = pd.Series(FLAT, index=df.index)
    for idx in df.index:
        cooled = (cooled_strategies or {}).get(idx, frozenset())
        if cooled:
            call_e = {n: p for n, p in call_elig.items() if n not in cooled}
            put_e = {n: p for n, p in put_elig.items() if n not in cooled}
            pred.loc[idx] = _pick(idx, signals, call_e, put_e)
        else:
            pred.loc[idx] = _pick(idx, signals, call_elig, put_elig)
    return pred, (call_elig, put_elig)


def walk_forward(df: pd.DataFrame, signals: dict[str, pd.Series], window: int = WF_WINDOW):
    """Rolling out-of-sample cascade using the common strategy roster."""
    pred = pd.Series(FLAT, index=df.index)
    for pos in range(window, len(df)):
        idx = df.index[pos]
        win = df.iloc[pos - window:pos]
        labels = pd.Series(_label_at(win, TARGET_THRESHOLD), index=win.index)
        cok = labels.isin([CALL, "BOTH"])
        pok = labels.isin([PUT, "BOTH"])

        call_elig, put_elig = {}, {}
        for name, sig in signals.items():
            try:
                meta = get_strategy_registry().get_meta(name)
            except KeyError:
                continue
            if not meta.can_hard_trade:
                continue
            w = sig.loc[win.index]
            fc, fp = w == CALL, w == PUT
            nc, npp = int(fc.sum()), int(fp.sum())
            if meta.direction in {CALL, "TWO_SIDED"}:
                call_elig[name] = int((fc & cok).sum()) / nc if nc else 0.0
            if meta.direction in {PUT, "TWO_SIDED"}:
                put_elig[name] = int((fp & pok).sum()) / npp if npp else 0.0

        pred.loc[idx] = _pick(idx, signals, call_elig, put_elig)
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
        "dir_recall": n_fired / n_move if n_move else float("nan"),
        "wrong_way_rate": wrong_way / n_fired if n_fired else float("nan"),
        "overall_accuracy": (int(correct.sum()) + correct_flat) / len(df_sub),
        "put_base": put_base,
        "lift": dir_prec / put_base if put_base else float("nan"),
    }


def score_directional_label(
    df_sub: pd.DataFrame,
    pred: pd.Series,
    label_column: str,
) -> dict[str, float | int]:
    """Return standard directional precision/recall/F1 for one label column."""
    labels = df_sub[label_column]
    call_ok = labels.isin([CALL, "BOTH"])
    put_ok = labels.isin([PUT, "BOTH"])
    fired = pred.isin([CALL, PUT])
    correct = ((pred == CALL) & call_ok) | ((pred == PUT) & put_ok)
    opportunities = call_ok | put_ok
    n_fired = int(fired.sum())
    n_correct = int(correct.sum())
    n_opportunities = int(opportunities.sum())
    precision = n_correct / n_fired if n_fired else float("nan")
    recall = n_correct / n_opportunities if n_opportunities else float("nan")
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision == precision and recall == recall and precision + recall > 0
        else float("nan")
    )
    return {
        "correct": n_correct,
        "fires": n_fired,
        "opportunities": n_opportunities,
        "precision": precision,
        "recall": recall,
        "f1": f1,
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
