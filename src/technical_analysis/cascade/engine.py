"""The cascade engine: scoring, registry-authorized voting, and the
walk-forward harness. Strategy-roster agnostic — `gather_regime_signals` takes the
roster (regime_families) as a parameter, so the experiment can pass the full roster
and production the registry-filtered subset while sharing this exact engine.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.technical_analysis.strategy_families import get_strategy_family_registry

from .constants import CALL, PUT, FLAT, REGIME_STRESS, REGIME_CALM, WF_WINDOW, COOLOFF_WINDOW, REGIME_THRESHOLD
from .dataset import _call_ok, _put_ok, _label_at


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
    """Production CALL/PUT voters for one regime.

    Strategy authority now comes from strategy_families.yaml: every
    TRADE_ELIGIBLE signal may vote directly. Historical side precision is still
    measured and carried as a tie-break/audit value, but it no longer gates live
    or backtest participation; manual promotion/demotion in the registry is the
    control point.
    """
    registry = get_strategy_family_registry()
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


# Families for which WATCH_ONLY variant misses also count toward the
# consecutive-wrong cooloff counter (in addition to TRADE_ELIGIBLE misses).
# These are mean-reversion/fade families where a watch fire in the wrong
# direction is as informative as a hard-cascade miss.
WATCH_COOLOFF_FAMILIES: frozenset[str] = frozenset({
    "PullbackCall", "BandReversion", "RsiReversion",
})


def build_family_vote_cascade(
    df: pd.DataFrame,
    regime_signals: dict[str, dict[str, pd.Series]],
    cooloff_families: dict | None = None,
) -> tuple[pd.Series, dict, dict]:
    """Family-level vote cascade with weak-opposition rule — Steps 1–2–4.

    One vote per distinct family (SIGNAL + VOTE_ONLY, suspended families excluded).
    Opposition to side S is WEAK when ≤ 1 opposing family AND none is SIGNAL.
    A single noisy VOTE_ONLY dissenter is not real disagreement.

    Decision table (CALL side; PUT is the mirror):
        C≥2, Cs≥1, weak_put_opp   → CALL trade
        C≥2, Cs==0, weak_put_opp  → VOTE_CONSENSUS CALL watch seed (full capital)
        C==1, Cs≥1, weak_put_opp  → CALL watch via add_watch_promotions (full capital)
        C==1, Cs==0, weak_put_opp → NEW: solo VOTE_ONLY CALL watch (half capital)
        both sides STRONG          → NO_POSITION (true conflict)
    Returns (final_pred, vote_only_seeds, solo_vote_only_seeds).
    """
    registry = get_strategy_family_registry()
    regimes = df["regime"]
    final_pred = pd.Series(FLAT, index=df.index)
    vote_only_seeds: dict = {}
    solo_vote_only_seeds: dict = {}

    for idx in df.index:
        regime = regimes.loc[idx]
        sigs = regime_signals.get(regime, {})
        cooled = (cooloff_families or {}).get(idx, frozenset())

        # Step 1: one vote per family; first firing variant represents the family.
        call_signal_fams: set[str] = set()
        put_signal_fams: set[str] = set()
        call_vote_only: dict[str, str] = {}   # family → representative variant name
        put_vote_only: dict[str, str] = {}
        seen: set[str] = set()

        for name, sig in sigs.items():
            try:
                meta = registry.get_meta(name)
            except KeyError:
                continue
            if not meta.can_vote:
                continue
            if meta.family in cooled or meta.family in seen:
                continue
            direction = sig.loc[idx] if idx in sig.index else FLAT
            if direction not in (CALL, PUT):
                continue
            seen.add(meta.family)
            is_signal = meta.strategy_type in {"SIGNAL", "TRADE_ELIGIBLE", "WATCH_ONLY"}
            if is_signal:
                (call_signal_fams if direction == CALL else put_signal_fams).add(meta.family)
            else:  # VOTE_ONLY
                (call_vote_only if direction == CALL else put_vote_only)[meta.family] = name

        C = len(call_signal_fams) + len(call_vote_only)
        P = len(put_signal_fams) + len(put_vote_only)
        # Opposition is WEAK when ≤ 1 opposing family AND none is a SIGNAL family.
        weak_put_opp  = (P <= 1) and (len(put_signal_fams) == 0)
        weak_call_opp = (C <= 1) and (len(call_signal_fams) == 0)

        # Step 2 + Step 4 — CALL side
        if C >= 2 and weak_put_opp:
            if call_signal_fams:
                final_pred.loc[idx] = CALL                 # trade
            else:
                fam, var = next(iter(call_vote_only.items()))
                vote_only_seeds[idx] = (CALL, fam, var)    # VOTE_CONSENSUS watch
        # PUT side (mirror)
        elif P >= 2 and weak_call_opp:
            if put_signal_fams:
                final_pred.loc[idx] = PUT
            else:
                fam, var = next(iter(put_vote_only.items()))
                vote_only_seeds[idx] = (PUT, fam, var)
        # Single-family SIGNAL watch seeds (add_watch_promotions handles lifecycle)
        elif C == 1 and call_signal_fams and weak_put_opp:
            pass  # SIGNAL CALL watch via add_watch_promotions
        elif P == 1 and put_signal_fams and weak_call_opp:
            pass  # SIGNAL PUT watch via add_watch_promotions
        # NEW: solo VOTE_ONLY + weak opposition → half-capital watch seed
        elif C == 1 and call_vote_only and not call_signal_fams and weak_put_opp:
            fam, var = next(iter(call_vote_only.items()))
            solo_vote_only_seeds[idx] = (CALL, fam, var)
        elif P == 1 and put_vote_only and not put_signal_fams and weak_call_opp:
            fam, var = next(iter(put_vote_only.items()))
            solo_vote_only_seeds[idx] = (PUT, fam, var)
        # Both sides STRONG → NO_POSITION (true conflict)

    return final_pred, vote_only_seeds, solo_vote_only_seeds


def compute_cooloff_families(
    df: pd.DataFrame,
    regime_signals: dict[str, dict[str, pd.Series]],
    cooloff_window: int | None = None,
) -> dict:
    """Return {index: set[family]} of families temporarily suspended.

    A family enters a ``cooloff_window``-day suspension (default: COOLOFF_WINDOW
    from constants) when any of its SIGNAL/TE/WO variants fires (non-FLAT) and
    is proven incorrect on 2 *consecutive* resolved signal dates.

    For families in WATCH_COOLOFF_FAMILIES, VOTE_ONLY variant misses also count.

    Shadow-grading: while suspended, if the family *would have* voted the correct
    direction, the suspension is lifted after that session (on the next signal date).
    """
    if cooloff_window is None:
        cooloff_window = COOLOFF_WINDOW

    registry = get_strategy_family_registry()

    family_variants: dict[str, list[tuple[str, pd.Series]]] = {}
    for regime_sigs in regime_signals.values():
        for name, sig in regime_sigs.items():
            try:
                meta = registry.get_meta(name)
            except KeyError:
                continue
            is_signal = meta.strategy_type in {"SIGNAL", "TRADE_ELIGIBLE", "WATCH_ONLY"}
            is_watch_sensitive_vote = (
                meta.strategy_type == "VOTE_ONLY" and meta.family in WATCH_COOLOFF_FAMILIES
            )
            if is_signal or is_watch_sensitive_vote:
                family_variants.setdefault(meta.family, []).append((name, sig))

    if not family_variants or "actual_trade_label" not in df.columns:
        return {}

    labels = df["actual_trade_label"].fillna("").tolist()
    n = len(df)
    cooloff: dict = {df.index[pos]: set() for pos in range(n)}

    for family, variants in family_variants.items():
        miss_streak = 0
        cooloff_remaining = 0  # sessions still suspended

        for pos in range(n):
            idx = df.index[pos]
            label = labels[pos]
            resolvable = label not in ("", "NO_POSITION", "BOTH", "nan")

            # Get family's representative vote for this position.
            family_vote = FLAT
            for name, sig in variants:
                if idx in sig.index:
                    v = sig.loc[idx]
                    if v in (CALL, PUT):
                        family_vote = v
                        break

            fired = family_vote in (CALL, PUT)
            correct = (
                resolvable and fired and (
                    (family_vote == CALL and label in (CALL, "BOTH"))
                    or (family_vote == PUT and label in (PUT, "BOTH"))
                )
            )
            wrong = fired and resolvable and not correct

            if cooloff_remaining > 0:
                # Suspended: add to map and shadow-grade.
                cooloff[idx].add(family)
                cooloff_remaining -= 1
                if correct:  # shadow hit — lift after this session
                    cooloff_remaining = 0
            else:
                # Active: track consecutive miss streak.
                if wrong:
                    miss_streak += 1
                    if miss_streak >= 2:
                        cooloff_remaining = cooloff_window
                        miss_streak = 0
                elif correct:
                    miss_streak = 0
                # No-fire days are neutral (miss_streak unchanged).

    return cooloff


def build_regime_cascade(
    df: pd.DataFrame,
    regime_signals: dict[str, dict[str, pd.Series]],
    elig_frames: dict[str, pd.DataFrame],
    cooloff_families: dict | None = None):
    """Legacy in-sample cascade kept for walk-forward summary and tests.
    New production code uses build_family_vote_cascade instead.
    """
    elig = {regime: _regime_eligibility(regime, sigs, elig_frames[regime])
            for regime, sigs in regime_signals.items()}
    registry = get_strategy_family_registry()
    regimes = df["regime"]
    pred = pd.Series(FLAT, index=df.index)
    for idx in df.index:
        regime = regimes.loc[idx]
        call_elig, put_elig = elig[regime]
        cooled = (cooloff_families or {}).get(idx, frozenset())
        if cooled:
            call_e = {n: p for n, p in call_elig.items()
                      if registry.get_meta(n).family not in cooled}
            put_e = {n: p for n, p in put_elig.items()
                     if registry.get_meta(n).family not in cooled}
            pred.loc[idx] = _pick(idx, regime_signals[regime], call_e, put_e)
        else:
            pred.loc[idx] = _pick(idx, regime_signals[regime], call_elig, put_elig)
    return pred, elig


def walk_forward_regime(df: pd.DataFrame,
                        regime_signals: dict[str, dict[str, pd.Series]],
                        window: int = WF_WINDOW):
    """Rolling out-of-sample regime cascade. For each day i (after a `window`
    warm-up), eligibility is fit only on the trailing `window` days that share day
    i's regime, then day i is predicted. Nothing from day i onward leaks in.

    Strategy eligibility uses regime-specific thresholds (STRESS=1%, CALM=0.5%)
    for internal precision scoring, keeping actual_trade_label in df ATR-based
    (for DB storage and precision/recall summaries)."""
    regimes = df["regime"]
    pred = pd.Series(FLAT, index=df.index)

    for pos in range(window, len(df)):
        idx = df.index[pos]
        regime = regimes.loc[idx]
        sigs = regime_signals.get(regime, {})
        win = df.iloc[pos - window:pos]
        win_same = win[win["regime"] == regime]
        if win_same.empty:
            win_same = win
        # Use regime-specific threshold for strategy eligibility (internal scoring only)
        threshold = REGIME_THRESHOLD.get(regime, 0.005)
        regime_labels = pd.Series(
            _label_at(win_same, threshold), index=win_same.index
        )
        cok = regime_labels.isin([CALL, "BOTH"])
        pok = regime_labels.isin([PUT, "BOTH"])

        call_elig, put_elig = {}, {}
        for name, sig in sigs.items():
            try:
                meta = get_strategy_family_registry().get_meta(name)
            except KeyError:
                continue
            if not meta.can_hard_trade:
                continue
            w = sig.loc[win_same.index]
            fc, fp = w == CALL, w == PUT
            nc, npp = int(fc.sum()), int(fp.sum())
            if meta.direction in {CALL, "TWO_SIDED"}:
                call_elig[name] = int((fc & cok).sum()) / nc if nc else 0.0
            if meta.direction in {PUT, "TWO_SIDED"}:
                put_elig[name] = int((fp & pok).sum()) / npp if npp else 0.0

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
        "dir_recall": n_fired / n_move if n_move else float("nan"),
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
