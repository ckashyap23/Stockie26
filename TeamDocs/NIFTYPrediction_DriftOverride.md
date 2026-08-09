# NIFTY Drift Override Logic

Post-cascade adjustment layer applied at **9:22–9:24 AM IST** after the first
5-minute NIFTY candle is available.  
Source: `src/technical_analysis/cascade/drift_overrule.py`

---

## Inputs

| Input | Source | Column |
|---|---|---|
| `nifty_drift_pct` | 9:15–9:20 AM candle: `close_920 / open_915 − 1` | `SignalFeatureDaily` |
| `nifty_gap_pct` | Overnight gap: `open_915(D) / close_1515(D-1) − 1` | `SignalFeatureDaily` |
| `gap_open_atr` | `nifty_gap_pct / (atr14 / close_1515)` — gap in ATR units | `SignalFeatureDaily` |
| `effective_prediction` | Cascade output (CALL / PUT / NO_POSITION) | `NiftyPrediction` |
| `watch_signal` | Active watch seeded by cascade (CALL_3D_WATCH etc.) | `NiftyPrediction` |
| `vix_chg_1d` | VIX change into D-1 close | `NiftyPrediction` |
| `global_asia_overnight_return_mean` | Asia overnight gap return | `NiftyPrediction` |
| `event_gate_reason` | Non-null = macro event day gate active | `NiftyPrediction` |
| `promotion_block_reason` | Contains "COOLOFF" = family in cooldown | `NiftyPrediction` |

**Derived**:
```
drift_dir = sign(nifty_drift_pct)   if |nifty_drift_pct| ≥ DRIFT_MIN (0.10%)
           else 0                   (tape too flat to have direction)
D = +1 for CALL direction, −1 for PUT direction
```

---

## Thresholds

| Constant | Value | Meaning |
|---|---|---|
| `DRIFT_MIN` | 0.10% | Minimum \|drift\| to count as directional |
| `GAP_CONFIRM_MIN` | 0.30% | \|gap\| threshold for "gap confirms" sub-case |
| `DRIFT_PROBE_MIN` | 0.15% | Minimum \|drift\| for NO_POSITION probe |
| `TAIL_SHOCK_ATR` | 1.5× | gap_open_atr threshold for tail-shock (currently **MUTED**) |
| `HALF_SIZE` | 0.50 | Position size fraction for half-size cases |

---

## Decision Table

### Branch 1 — Cascade fired CALL or PUT

| Condition | Drift Result | Size | Reason Code |
|---|---|---|---|
| `drift_dir == D` **AND** `\|gap\| ≥ 0.30%` | Same direction | **0.5×** | `DRIFT_CONFIRMS_HALF_SIZE` |
| `drift_dir == D` (gap too small) | Same direction | **full** | `DRIFT_CONFIRMS_FULL` |
| `drift_dir ≠ D` OR `\|drift\| < 0.10%` | **Unchanged** | full | `DRIFT_NONE_NO_CHANGE` |

> **Note**: `DRIFT_OPPOSES` has been removed. Drift can only confirm (full or half) or stay silent.
> The cascade direction is always preserved regardless of what the tape does.

---

### Branch 2 — Watch active (cascade has a pending watch, not yet promoted)

Condition: `watch_signal` is set AND `effective_prediction = NO_POSITION` AND `promoted_prediction = NO_POSITION`.

| Condition | Drift Result | Size | Reason Code |
|---|---|---|---|
| `drift_dir == watch_direction` | Promote to CALL or PUT | **0.5×** | `DRIFT_PROMOTES_WATCH` |
| Otherwise | NO_POSITION | 0 | `WATCH_NO_DRIFT_CONFIRM` |

> Drift acts as the confirming signal for a watch that hasn't been confirmed by a
> second strategy family. Only half-size because the cascade itself didn't commit.

---

### Branch 3 — Cascade NO_POSITION, no active watch

**Path 1 — Drift-led probe** (`DRIFT_PROBE`):

```
|nifty_drift_pct| ≥ DRIFT_PROBE_MIN_PCT   (env var, default 0.15%)
AND NOT event_gate_reason                  (not a macro event day)
AND NOT is_family_suspended                (no strategy family in COOLOFF)
→ TRADE  direction = CALL if drift > 0, PUT if drift < 0
  size   = base (full)  if sign(drift) == sign(gap) AND gap ≠ 0   ← gap confirms
  size   = HALF_SIZE    otherwise                                  ← drift-only signal
```

> Probe fires on **drift alone** — gap alignment is no longer required to fire.
> Gap alignment only determines conviction (full vs half size).

**Path 2 — Tail-shock** (currently **MUTED** — commented out in code):

```
|gap_open_atr| > 1.5               (gap > 1.5× one ATR)
AND vix_chg_1d > 0                 (VIX was rising into D-1 close)
AND sign(global_asia_overnight) == sign(gap)   (Asia confirms gap direction)
AND NOT event day
→ TRADE  direction = sign(gap)
  size   = HALF_SIZE
```
> Muted after backtesting showed 25% precision over the full history — net negative.

**Fallback**:
```
→ NO_POSITION   size = 0   [NO_CHANGE]
```

---

## Outputs stored in `NiftyPrediction`

| Column | Type | Meaning |
|---|---|---|
| `drift_effective_prediction` | varchar(20) | Final direction: `CALL / PUT / NO_POSITION` |
| `drift_position_size_pct` | float | Size as fraction of capital (0.5 = half, 1.0 = full, 0 = no trade) |
| `drift_overrule_reason` | varchar(120) | One of the reason codes above |

The original `effective_prediction` is **never modified** — drift results are
stored separately and used as `direction_override` in option selection and paper entry.

---

## Empirical Impact (637 graded rows, 2024-01-01 → 2026-07-29, new logic)

### Q1 — Cascade wrong, drift responded (10 rows)
All 10 kept as `DRIFT_NONE_NO_CHANGE` (drift confirms or passes through). **0 saves, 0 kills** — no suppression possible without DRIFT_OPPOSES.

### Q2 — Cascade right, drift kept (33 rows)
**0 harmed** — drift never suppresses cascade direction anymore. All 33 correct trades preserved.

### Q3 — Cascade NO_POSITION, actual moved (558 actual moves)

| Drift reason | Fired | Correct | Wrong | Precision |
|---|---|---|---|---|
| `DRIFT_PROBE` | 170 | 133 | 37 | **78.2%** |
| `DRIFT_PROMOTES_WATCH` | 45 | 37 | 8 | **82.2%** |
| **Total** | **215 / 558** | **170** | **45** | **79.1%** |

Coverage: **38.5%** of missed actual moves (vs 19% with old logic).

### Net impact

```
net = +0 Q1saves − 0 Q2kills + 170 Q3captures − 45 Q3false_alarms = +125
```

### Before vs After

| Metric | Old logic | New logic |
|---|---|---|
| Drift fires | 106 | **215** |
| Recall | 18% | **38.5%** |
| Precision | 81.1% | **79.1%** |
| Net improved rows | +60 | **+125** |

---

## Tuning Levers

| Parameter | Current | Effect of increasing | Effect of decreasing |
|---|---|---|---|
| `DRIFT_MIN` | 0.10% | Fewer DRIFT_OPPOSES kills (weaker tape ignored) | More suppression of correct trades |
| `GAP_CONFIRM_MIN` | 0.30% | Fewer HALF_SIZE cases (gap must be larger to confirm) | More trades reduced to half size |
| `DRIFT_PROBE_MIN` | 0.15% | Fewer DRIFT_PROBE fires (higher confidence bar) | More probe trades, lower precision |
| `HALF_SIZE` | 0.50 | — | Smaller allocation for half-size cases |

> **Recommended next experiment**: raise `DRIFT_MIN` from 0.10% to 0.15–0.20% for the
> `DRIFT_OPPOSES` branch only (i.e. require a stronger opposing tape to suppress a
> cascade trade). This would reduce Q2 kills without significantly affecting Q3 coverage.
