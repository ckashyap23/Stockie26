"""Turns the signed news score into an expected return, then into the 0-1 magnitude.

The news score is a *feature*, not a forecast. Target (fixed, documented): the close-to-close return
of the target session versus the previous session's close, as a fraction (0.01 = +1%).

    predicted_return = intercept + slope * score
    magnitude_0_1    = min(|predicted_return| / training_scale, 1)
    training_scale   = 95th percentile of |return| over the training sessions (stored with the fit)

Fitted on ALL trading sessions (not only >0.5% days) and validated walk-forward, so the reported
hit-rate is out-of-sample. Until a calibration file exists the API reports the raw score and labels
itself uncalibrated.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

TARGET = "previous close -> close return of target_date (fraction)"


@dataclass
class Calibration:
    intercept: float
    slope: float
    training_scale: float
    n_train: int
    fitted_through: str  # last session date used
    target: str = TARGET
    validation: dict = field(default_factory=dict)

    def predicted_return(self, score: float) -> float:
        return self.intercept + self.slope * score

    def magnitude(self, predicted_return: float) -> float:
        return min(abs(predicted_return) / self.training_scale, 1.0) if self.training_scale > 0 else 0.0

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Calibration | None":
        if not path.exists():
            return None
        return cls(**json.loads(path.read_text(encoding="utf-8")))


def ols(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Returns (intercept, slope); slope 0 when x has no variance."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return my, 0.0
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return my - slope * mx, slope


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    for r, i in enumerate(order):
        ranks[i] = float(r)
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float:
    rx, ry = _rank(xs), _rank(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return sum((a - mx) * (b - my) for a, b in zip(rx, ry)) / (sx * sy) if sx and sy else 0.0


def fit(rows: list[tuple[str, float, float]], min_rows: int = 60, folds: int = 5) -> Calibration:
    """rows = [(session_date, news_score, return_fraction)], any order. Raises ValueError if too few."""
    rows = sorted(rows)
    if len(rows) < min_rows:
        raise ValueError(f"need >= {min_rows} sessions with both a stored news score and a return; have {len(rows)}")
    scores = [r[1] for r in rows]
    rets = [r[2] for r in rows]

    # Walk-forward: fit on everything before each block, test on the block (chronological, no leakage).
    start = len(rows) // 2
    step = max(1, (len(rows) - start) // folds)
    hits = tested = 0
    err_model = err_zero = 0.0
    oos_pred: list[float] = []
    oos_true: list[float] = []
    for lo in range(start, len(rows), step):
        a, b = ols(scores[:lo], rets[:lo])
        for s, r in zip(scores[lo:lo + step], rets[lo:lo + step]):
            p = a + b * s
            oos_pred.append(p)
            oos_true.append(r)
            tested += 1
            hits += (p > 0) == (r > 0)
            err_model += abs(p - r)
            err_zero += abs(r)

    intercept, slope = ols(scores, rets)
    return Calibration(
        intercept=intercept,
        slope=slope,
        training_scale=percentile([abs(r) for r in rets], 0.95),
        n_train=len(rows),
        fitted_through=rows[-1][0],
        validation={
            "scheme": f"expanding-window walk-forward, {folds} blocks from the midpoint",
            "n_oos": tested,
            "sign_hit_rate": round(hits / tested, 4) if tested else None,
            "mae_model": round(err_model / tested, 6) if tested else None,
            "mae_zero_forecast": round(err_zero / tested, 6) if tested else None,
            "rank_ic": round(spearman(oos_pred, oos_true), 4) if tested > 2 else None,
        },
    )
