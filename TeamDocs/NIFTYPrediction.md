# NIFTY Prediction Workflow

End-to-end pipeline from raw features to `drift_effective_prediction` and paper trade entry.

---

## Phase 0 — Feature Assembly

**Source**: `UnderlyingSnapshot` (daily OHLC) → `SignalFeatureDaily` (feature store)  
**Script**: `scripts/Common/calculate_underlying_features.py`

| Feature Group | Key Columns |
|---|---|
| Price / returns | `close_1515`, `open_915`, `ret_2d/3d/5d/10d/20d`, `next_return_pct` |
| Moving averages | `ma5d_slope`, `ma10d_slope`, `ma20_slope`, `ma_slope_combo` |
| Oscillators | `rsi5`, `rsi14`, `bb_width`, `bb_upper/lower` |
| Volatility | `atr7`, `atr14`, `volatility_10d/20d` |
| Volume | `volume_hybrid` (today / 20d avg) |
| Range / S-R | `range_position_5d/10d/20d`, `support_level_10d`, `resistance_level_10d`, `resistance_distance_10d`, `support_broken_10d`, `resistance_broken_10d` |
| Regime input | `vix_close`, `vix_chg_1d`, `vix_chg_pct` |
| Regime | `calm` or `stress` (VIX + volatility thresholds) |

**Global index features** (`GlobalIndexOhlc` → computed by `build_global_index_features_cumulative`):

| Column | Definition |
|---|---|
| `global_us_return_mean` | Mean open→close return of US session (D-1), D-1 relative to D-2 |
| `global_europe_return_mean` | Same for EUR session |
| `global_asia_overnight_return_mean` | Asia: D-1 final close → D market open (overnight gap) |
| `global_asia_partial_return_mean` | Asia: D market open → 9:20 AM IST partial close (intraday tape) |
| `global_return_mean` | Mean across all risk regions (US + EUR + Asia) |
| `global_breadth` | (positive regions − negative) / total; feeds `global_risk_on/off` |

**Open-gap features** (`SignalFeatureDaily` via `daily_open_gap.py`, 9:22 AM IST):

| Column | Formula |
|---|---|
| `nifty_gap_pct` | `nifty_open_915(D) / close_1515(D-1) − 1` |
| `nifty_drift_pct` | `nifty_close_920(D) / nifty_open_915(D) − 1` (first 5 min tape) |
| `gift_gap_pct` | `gift_920(D) / gift_1515(D-1) − 1` (GIFT NIFTY vs its own prev close) |
| `gap_confirmed` | `sign(nifty_gap) == sign(gift_gap)` and gap ≠ 0 |
| `gap_fade` | gap ≠ 0 and drift ≠ 0 and `sign(drift) ≠ sign(gap)` |
| `gap_open_atr` | `nifty_gap_pct / (atr14 / close_1515)` — gap in ATR units |
| `gift_gap_atr` | `gift_gap_pct / (atr14 / close_1515)` |

---

## Phase 1 — Individual Strategy Signals

**Source**: `src/technical_analysis/cascade/strategies.py`  
**Config**: `src/technical_analysis/strategy_families.yaml`

Each strategy variant evaluates features and emits `CALL / PUT / FLAT` per regime day.

| Authority | Examples | Role |
|---|---|---|
| `SIGNAL` | `PullbackCall_TrendIntact`, `DeclineContinuationPut_ATR`, `ExpansionVotes_*` | Seeds watches; drives hard cascade trades |
| `VOTE_ONLY` | `RsiReversion_6040`, `TrendDownPut` | Casts family-level votes; cannot trade or seed watches directly |
| `RESEARCH` | `GlobalShockPut_AsiaRoom`, `FastDropPut_5d` | Research grid only; no production participation |

**Guard variants** (`_GlobalAllDisagree`, `_GlobalAsiaDisagree`, `_GlobalAsiaAgree`) suppress signals when `global_asia_overnight_return_mean` contradicts the trade direction.

---

## Phase 2 — 6-Step Family-Vote Cascade

**Source**: `src/technical_analysis/cascade/engine.py`

Runs per regime per signal date on `SIGNAL` + `VOTE_ONLY` families:

```
Step 1  Count CALL/PUT votes by family
Step 2  Weak-opposition check:
          weak  = ≤1 VOTE_ONLY dissenter, no SIGNAL dissenter
          strong = ≥2 families or any SIGNAL on the opposite side
Step 3  ≥2 SIGNAL families agree + weak opposition  →  hard CALL or PUT
Step 4  Only VOTE_ONLY families align               →  no hard trade; seed VOTE_ONLY watch
Step 5  Strong opposition                           →  FLAT regardless
Step 6  Global gate: suppress trade if global_risk_off AND same-side global weakness
          uses global_asia_overnight_return_mean inside GLOBAL_REGION_COLS
```

Output: **`final_prediction`** = `CALL / PUT / NO_POSITION`

---

## Phase 3 — Watch Promotion

**Source**: `src/technical_analysis/cascade/watch_promotion.py`

A watch is created on D0 when a single SIGNAL family fires without a second confirmer.
Promotion requires a **different** family to confirm on D1 or D2.

| Event | Condition | Outcome |
|---|---|---|
| Watch created | SIGNAL fires, ≥2 families don't agree | `CALL_3D_WATCH` or `PUT_3D_WATCH` |
| Promoted | Different family confirms same side + weak opposition | `promoted_prediction = CALL/PUT` |
| Same-family re-fire | Seeder re-fires alone | Blocked — not a confirmation |
| Strong opposition | Any SIGNAL on opposite side | Watch killed immediately |
| Cooloff | Seeder family in cooldown | `WATCH_PROMOTION_BLOCKED_COOLOFF` |
| Horizon exceeded | D2 without confirmation | `WATCH_EXPIRED_NO_CONFIRMATION` |
| Price-action promotion | — | **Disabled** (`watch_only_price_action_promotion: false`) |

Output: `watch_signal`, `promoted_prediction`, `promotion_reason`, `confirming_family`

---

## Phase 4 — Effective Prediction + Event Gate

```
effective_prediction = final_prediction      # hard cascade trade
                    or promoted_prediction   # watch promoted by different family
                    or NO_POSITION           # all flat
```

**Event gate**: if `is_event_impact_day(signal_date)` → override to `NO_POSITION` even if cascade fired. Reason stored in `event_gate_reason`.

Output: **`effective_prediction`** persisted to `NiftyPrediction`

---

## Phase 5 — Drift Overrule

**Source**: `src/technical_analysis/cascade/drift_overrule.py`  
**Runs**: inside `daily_nifty_signal.py` at 9:22 AM IST, after open-gap features are available.

Reads `effective_prediction` + gap features and applies a thin pre-market adjustment.
The cascade output is **never modified** — results are stored separately.

```
drift_dir = sign(nifty_drift_pct)  if |nifty_drift_pct| ≥ 0.10%  else NONE
D = +1 for CALL direction, -1 for PUT direction

TRADE (CALL or PUT):
  drift == D  AND  |nifty_gap_pct| > 0.30%  →  TRADE   size = 0.5×  [DRIFT_CONFIRMS_HALF_SIZE]
  drift == D                                →  TRADE   size = full  [DRIFT_CONFIRMS_FULL]
  drift == −D                               →  NO_POS  size = 0     [DRIFT_OPPOSES]
  |drift| < 0.10%                           →  unchanged            [DRIFT_NONE_NO_CHANGE]

WATCH (watch_signal set, effective_prediction = NO_POSITION, not yet promoted):
  drift == watch_dir                        →  promote → TRADE 0.5× [DRIFT_PROMOTES_WATCH]
  otherwise                                 →  unchanged

NO_POSITION — Path 1 (drift-led probe):
  |drift| ≥ 0.15%
  AND sign(drift) == sign(gap)
  AND not is_event_impact_day
  AND not family_suspended (no active cooloff)
  → TRADE (CALL if drift > 0, PUT if drift < 0)  0.5×              [DRIFT_PROBE]

NO_POSITION — Path 2 (tail-shock, fires even when drift is small):
  |gap_open_atr| > 1.5
  AND vix_chg_1d(D-1) > 0   (VIX was rising into yesterday's close)
  AND sign(global_asia_overnight_return_mean) == sign(gap)
  AND not is_event_impact_day
  → TRADE (direction = sign(nifty_gap_pct))  0.5×                  [TAIL_SHOCK]
```

Output stored in `NiftyPrediction`:

| Column | Meaning |
|---|---|
| `drift_effective_prediction` | Final direction after overrule: `CALL / PUT / NO_POSITION` |
| `drift_position_size_pct` | Position size as fraction of capital (0.5 = half, 1.0 = full) |
| `drift_overrule_reason` | Reason code from the table above |

---

## Phase 6 — Option Selection + Paper Entry

**Script**: `daily_nifty_signal.py` → `daily_option_selection.py` → `optionselection/pipeline.py`

`drift_effective_prediction` is passed as `direction_override` to `run_option_selection_from_db`.  
When drift promotes a `NO_POSITION` to a trade, the standard ATM option is selected (no cascade strategy attached).  
`drift_position_size_pct` flows into `NiftyOptionSelection` for position sizing in `daily_paper_entry.py`.

---

## Daily Cron Chain

| Time (IST) | Script | Purpose |
|---|---|---|
| 3:00 AM | `load_daily_index_data --mode us-eur` | D-1 US/EUR complete OHLC |
| 9:00 AM | `load_daily_index_data --mode asia-partial` | D Asia partial OHLC (open → 9:20 AM) |
| **9:20 AM** | `daily_NIFTYGift_snapshot --mode open` | gift_920 → `GiftNiftySnapshot` |
| **9:22 AM** | `daily_open_gap` | 7 gap features → `SignalFeatureDaily` for D-1; drift overrule also fires here |
| **9:24 AM** | `daily_nifty_signal --model-version cascade_v1` | Cascade + drift overrule + option selection; requires gap features from 9:22 |
| **9:28 AM** | `daily_paper_entry --underlying NIFTY --max-stale-seconds 300` | Execute paper trade; falls back to NO_POSITION if gap features missing |
| 3:15 PM | `daily_NIFTYGift_snapshot --mode close` | gift_1515 → `GiftNiftySnapshot` (tomorrow's D-1 reference) |
| 3:47 PM | `daily_market_refresh --underlying NIFTY` | EOD OHLC → auto-chains prediction pipeline |
| 4:00 PM | `daily_NIFTYoption_OHLC --underlying NIFTY` | Option OHLC → auto-chains option-selection + PnL |

> **Critical ordering**: 9:20 → 9:22 → 9:24 → 9:28. If `daily_open_gap` (9:22) is skipped, the drift overrule
> cannot fire and `daily_nifty_signal` falls back to the cascade `effective_prediction` only.
> If the cascade had `NO_POSITION`, no paper trade will be entered that day.
