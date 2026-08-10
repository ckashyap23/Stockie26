# Production Strategy Rules — CALL / PUT / NO_POSITION

Complete decision chain: **Regime → Strategies → Cascade → Watch → Drift**

---

## Step 1 — Regime Classification

| Regime | Condition |
|---|---|
| **CALM** | `vix_close < 16` **AND** `volatility_10d < 0.7%` |
| **STRESS** | Everything else |

The regime gates which strategies fire and sets thresholds for `actual_trade_label` grading:

| Regime | NIFTY move required (from `next_open`, over `UNDERLYING_LOOKBACK_DAYS`) |
|---|---|
| STRESS | `STRESS_NIFTY_TARGET_PCT = 0.5%` |
| CALM | `CALM_NIFTY_TARGET_PCT = 0.2%` |

---

## Step 2 — Strategy Rules (CALL Side)

### PullbackCall family — `SIGNAL` (can seed a trade directly)

| Variant | Conditions |
|---|---|
| **QuietVol** | `range_position_20d ≤ 0.25` AND `vix ≤ 14` (stress) / `≤ 13` (calm) |
| **DeepWashout** | Same as QuietVol AND `rsi5 ≤ 30` |
| **TrendIntact** | `ma20_slope ≥ 0.3%` AND `range_position_10d ≤ 0.20` AND `resistance_distance_10d ≥ 1.5%` AND `support_broken_10d = false` |
| **TrendRest** | `ma20_slope > 0` AND `ma10d_slope ≤ 0` AND `resistance_distance_10d ≥ 1.5%` AND `bb_width ≥ 4%` AND `support_broken_10d = false` |

### ExpansionVotes — `SIGNAL`, STRESS-only, two-sided

CALL fires when: `vix ≥ 16` AND `bb_width ≥ 6.5%` AND ≥ 3 momentum votes for CALL direction.

### RsiReversion — `VOTE_ONLY` (can confirm, cannot trade alone)

CALL fires when: `rsi14 ≤ 40`

---

## Step 3 — Strategy Rules (PUT Side)

| Strategy | Conditions |
|---|---|
| **DeclineContinuationPut_ATR** | `ret_3d ≤ −0.5×(atr14/close)` AND `ma5d_slope < 0` AND `range_position_10d ≥ 0.20` AND `bb_width ≥ 4%` |
| **BreakdownPut_20d** *(STRESS only)* | `close ≤ min(low[−20:−1])` AND `bb_width ≥ 6.5%` AND `support_broken_10d = true` |
| **ExpansionVotes** *(STRESS only)* | Same expansion frame as CALL, ≥ 3 PUT votes; suppressed if `rsi5 < 30` (oversold) or price near support |
| **TrendDownPut** *(VOTE_ONLY)* | `ma20_slope ≤ −0.3%` AND `volume ≥ min(90k, 1.2×vol_20d)` AND `vix ≥ 12` |
| **RsiReversion** *(VOTE_ONLY)* | `rsi14 ≥ 60` |

---

## Step 4 — Cascade Aggregation

| Outcome | Condition |
|---|---|
| **`final_prediction = CALL`** | ≥ 2 families vote CALL **AND** weak PUT opposition (≤ 1 VOTE_ONLY, no PUT SIGNAL) **AND** ≥ 1 SIGNAL family on CALL side |
| **`final_prediction = PUT`** | Symmetric to CALL |
| **Watch seeded** (not traded yet) | 1 SIGNAL family votes CALL/PUT, weak opposition → `watch_signal = CALL_3D_WATCH / PUT_3D_WATCH`; up to 2 days for confirmation |
| **`final_prediction = NO_POSITION`** | Both sides strong (conflict), or gates block, or no strategies fire |

---

## Step 5 — Gates That Suppress CALL/PUT → NO_POSITION

| Gate | Condition | Effect |
|---|---|---|
| **Gap guard** | `next_open / close_1515 > 1.003` (gap UP > 0.3%) for CALL | CALL suppressed — already gapped away |
| **Gap guard** | `close_1515 / next_open > 1.003` (gap DOWN > 0.3%) for PUT | PUT suppressed |
| **Event gate** | Macro-event day in calendar | Selective family suppression or full flatting |
| **Family cooloff** | Family had 2 consecutive wrong signal dates | Suspended for 5 sessions; cannot fire or confirm |

---

## Step 6 — Watch Promotion (D1/D2)

If D0 = NO\_POSITION but a watch was seeded, a CALL/PUT is promoted on D1 or D2 when **all** of:

1. A **different family** (≠ seeder) votes the same direction
2. Opposition remains weak (≤ 1 VOTE_ONLY, no SIGNAL on opposite side)
3. Both seeder family and confirmer family are **not in cooloff**
4. Watch age ≤ 2 trading days

Result: `effective_prediction = CALL/PUT` (promoted), `promoted_prediction = CALL/PUT`

> Watch is killed immediately if strong opposition fires (2+ families or any SIGNAL on opposite side).

---

## Step 7 — Drift Overrule (9:22 AM IST, pre-market)

Applied to `effective_prediction`. Reads:
- `nifty_drift_pct` — intraday drift from D-1 9:15–9:20 candle
- `nifty_gap_pct` — D-1 close-to-D open gap

Thresholds (from `.env`):

| Variable | Default | Meaning |
|---|---|---|
| `DRIFT_PROBE_MIN_PCT` | 0.15% | Minimum \|drift\| to fire a NO_POSITION probe |
| `DRIFT_PROBE_HALF_MIN_PCT` | 0.20% | Minimum \|drift\| for a **half-size** probe (gap absent or opposes) |
| `GAP_CONFIRM_MIN` | 0.30% | \|gap\| required for gap-confirmed sizing |

### Branch A — Existing TRADE (`effective_prediction = CALL or PUT`)

| Drift vs direction | Gap confirmed? | Result |
|---|---|---|
| Aligns with prediction | Yes (\|gap\| ≥ 0.3%) | Keep direction, **HALF_SIZE** — `DRIFT_CONFIRMS_HALF_SIZE` |
| Aligns with prediction | No | Keep direction, **full size** — `DRIFT_CONFIRMS_FULL` |
| Opposes prediction | — | Keep direction, full size — `DRIFT_NONE_NO_CHANGE` |

### Branch B — Watch exists, NO_POSITION day

| Drift vs watch direction | Result |
|---|---|
| Confirms watch | **CALL/PUT at HALF_SIZE** — `DRIFT_PROMOTES_WATCH` |
| Does not confirm | **NO_POSITION** — `WATCH_NO_DRIFT_CONFIRM` |

### Branch C — Pure NO_POSITION probe (no watch, no cascade signal)

| Condition | Result |
|---|---|
| \|drift\| ≥ 0.15% **AND** gap aligns (same sign, \|gap\| ≥ 0.3%) | **CALL/PUT at full size** — `DRIFT_PROBE` (gap-confirmed) |
| \|drift\| ≥ **0.20%** AND gap absent or opposes | **CALL/PUT at HALF_SIZE** — `DRIFT_PROBE` (weak) |
| 0.15% ≤ \|drift\| < 0.20% AND gap absent or opposes | **NO_POSITION** — filtered by `DRIFT_PROBE_HALF_MIN_PCT` gate |
| \|drift\| < 0.15% | **NO_POSITION** — below minimum threshold |
| Event gate active | **NO_POSITION** — blocked |
| Family suspended (cooloff) | **NO_POSITION** — blocked |

---

## Summary — When CALL fires

```
CALL = ANY of:

1. CASCADE DIRECT TRADE
   PullbackCall (any variant) fires
   AND ≥ 2 total families vote CALL
   AND no PUT SIGNAL family opposing
   AND no event gate / gap guard active

2. WATCH → PROMOTED
   1 CALL SIGNAL family seeded watch (D-1 or D-2)
   AND different family confirms CALL today
   AND both families not in cooloff
   AND drift confirms or is neutral at 9:22 AM

3. DRIFT_PROBE (full size)
   effective_prediction = NO_POSITION (cascade flat)
   AND nifty_drift_pct ≥ +0.15%
   AND gap is also positive AND |gap| ≥ 0.3%
   AND no event gate, no family suspension

4. DRIFT_PROBE (half size)
   effective_prediction = NO_POSITION
   AND nifty_drift_pct ≥ +0.20%
   AND gap absent or negative (weak signal)
   AND no event gate, no family suspension
```

---

## Key Config Variables (`.env`)

| Variable | Default | Controls |
|---|---|---|
| `STRESS_NIFTY_TARGET_PCT` | 0.005 | NIFTY move for stress `actual_trade_label` grading |
| `CALM_NIFTY_TARGET_PCT` | 0.002 | NIFTY move for calm `actual_trade_label` grading |
| `STRESS_TARGET_PCT` | 0.10 | Option premium target (10%) — stress trades |
| `CALM_TARGET_PCT` | 0.07 | Option premium target (7%) — calm trades |
| `STRESS_SL_PCT` | 0.05 | Stop loss (5%) — stress trades |
| `CALM_SL_PCT` | 0.03 | Stop loss (3%) — calm trades |
| `STRESS_VIX_QUIET_MAX` | 14 | VIX ceiling for PullbackCall QuietVol (stress) |
| `CALM_VIX_QUIET_MAX` | 13 | VIX ceiling for PullbackCall QuietVol (calm) |
| `STRESS_BB_WIDTH_MIN` | 0.04 | Min BB width for expansion strategies (stress) |
| `CALM_BB_WIDTH_MIN` | 0.04 | Min BB width for expansion strategies (calm) |
| `DRIFT_PROBE_MIN_PCT` | 0.0015 | Min drift to fire any probe |
| `DRIFT_PROBE_HALF_MIN_PCT` | 0.002 | Min drift for half-size probe (gap not confirming) |
| `STRESS_SL_DIVIDER` | 5 | Cascade ratchet SL divider — stress |
| `CALM_SL_DIVIDER` | 10 | Cascade ratchet SL divider — calm |
| `N_CAP` | 5 | Max cascade ratchet levels |
| `TRADE_HORIZON_DAYS` | 1 | Option holding days (PnL simulation / paper trade) |
| `UNDERLYING_LOOKBACK_DAYS` | 1 | Days for `actual_trade_label` grading horizon |
