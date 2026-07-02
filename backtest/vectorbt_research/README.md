# VectorBT Research (Type 2 Backtesting)

Current behavior: options are selected by ITM delta rules, not simple ATM
distance. Replay uses `TRADE_HORIZON_DAYS`; signal quality uses the independent
`UNDERLYING_LOOKBACK_DAYS` NIFTY window.

This folder is the **research pipeline backtesting** layer — testing prediction
signal strategies (promoted and experimental) against ATM option replays, with
regime routing applied.

For the other two backtesting types see [`backtest/README.md`](../README.md).

| File | Purpose |
|---|---|
| `strategy_grid.py` | Define and run signal variants → ATM option replay → PnL leaderboard |
| `regime_experiment.py` | Compare regime-detection variants (calm vs stress routing) |

---

## Quick start

```powershell
# All variants from April 2025
python -m backtest.vectorbt_research.strategy_grid --start 2025-04-01

# Filter by name substring (case-insensitive, comma-separated)
python -m backtest.vectorbt_research.strategy_grid --variants Momentum,CalmTrend

# With stop-loss, custom date range
python -m backtest.vectorbt_research.strategy_grid --start 2025-06-01 --stop-loss-pct 0.015

# All flags
python -m backtest.vectorbt_research.strategy_grid --help
```

Outputs go to `output/backtest/NIFTY/vectorbt_research/`:

| File | Contents |
|---|---|
| `strategy_grid_leaderboard.csv` | One row per variant, ranked by total PnL |
| `strategy_grid_trades.csv` | Every individual trade with entry/exit price and PnL |
| `strategy_grid_trade_plans.csv` | Which ATM option was selected for each signal |
| `strategy_grid_definitions.csv` | Name + description for every variant that ran |
| `strategy_grid_summary.txt` | Plain-text leaderboard |

---

## How strategies work

Every strategy is a `StrategyVariant` — a name, a description, and a signal function:

```python
@dataclass(frozen=True)
class StrategyVariant:
    name: str         # unique ID; appears in leaderboard and output files
    signal_fn: fn     # (df: pd.DataFrame) -> pd.Series[str]
    description: str  # written to strategy_grid_definitions.csv
```

The `signal_fn` receives the NIFTY feature store (one row per trading day) with
a `regime` column already added (`"calm"` or `"stress"`, using the same VIX/vol
thresholds as the production cascade). It must return a `pd.Series` of the same
length with values `"CALL"`, `"PUT"`, or `"NO_POSITION"`.

The grid engine then:
1. Selects the current ITM delta candidate from the DB for each signal date
2. Replays intraday snapshots to simulate entry/exit at a fixed target % or stop
3. Computes PnL per unit and per lot

---

## Currently defined variants

### Promoted cascade variants

These mirror the production prediction pipeline signals exactly.

| Name | Side | Summary |
|---|---|---|
| `MomentumDirectional_CallExpGuard` | CALL | **Production signal.** MomentumDirectional >=2 votes AND `bb_width >= 5.5%` |
| `MomentumDirectional` | Both | Base two-sided: CALL on >=2 oversold votes, PUT on >=3 down-momentum votes |
| `OversoldBounceCall_ContextRoom` | CALL | RSI <= dynamic rolling cap + resistance room + VIX >= 12 (stress) |
| `DownMomentumPut_MoreTrades` | PUT | `ma20_slope <= -0.3%` + adaptive volume floor + VIX >= 12 (stress) |
| `RsiMeanReversion_6040` | Both | RSI < 40 → CALL, RSI > 60 → PUT |
| `BollingerMeanReversion` | Both | Close below/above 2-sigma Bollinger band |

### Experimental cascade variants

These exist in `src/technical_analysis/cascade/strategies.py` but have not yet
cleared the production precision floor. Calm-regime strategies are automatically
suppressed on stress dates via `_gate_to_regime()`.

**Stress regime:**

| Name | Side | Summary |
|---|---|---|
| `OversoldBounceCall_HighPrecision` | CALL | range_position_10d <= 20th pctile + VIX >= 12 (highest precision, fewer trades) |
| `OversoldBounceCall_MoreTrades` | CALL | rsi14 <= 42 + resistance room >= 2.5% + VIX >= 12 |
| `DownMomentumPut_HighPrecision` | PUT | ma20_slope <= -0.3% + volume floor + delta VIX > 0 |
| `DownMomentumPut_Fast` | PUT | ma5_slope <= -0.2% + ret_3d <= -0.5% (earlier entry) |
| `MomentumDirectional_ContextVotes_ExpansionGuard` | Both | Two-sided + bb_width >= 5.5% + resistance room >= 1.5% |
| `MomentumDirectional_ContextVotes_StrongExpansionGuard` | Both | Two-sided + VIX >= 16 + bb_width >= 6.5% (tightest) |
| `MAAlignmentRoom_ReboundCall` | CALL | MA5/MA10/MA20 upward alignment + resistance room |
| `MAAlignmentRoom_PutGuarded` | PUT | MA stack inverted + range_position < 50% + ret_5d < 0 |
| `MaTrend_001` | Both | MA10/MA20 spread > 0.1% → CALL, < -0.1% → PUT |
| `RangeBreakout` | Both | Close breaks prior session high/low |
| `RangeBreakout_ATRBuffer` | Both | Breakout entry inside range by 0.15*ATR (earlier signal) |

**Calm regime (suppressed on stress dates):**

| Name | Side | Summary |
|---|---|---|
| `CalmTrendCall_ContextHeadroom` | CALL | MA20 uptrend + dynamic resistance room + trend efficiency >= 25% |
| `CalmTrendCall_Pullback` | CALL | MA20 uptrend + range_position_10d <= 50% + trend efficiency >= 25% |
| `CalmFadePut_ContextOverbought` | PUT | rsi14 and rsi5 both exceed rolling overbought floor |
| `CalmMomentumPut_Continuation` | PUT | ret_3d <= -0.3% (falling momentum continues) |

### Parametric sweep variants

| Name | Summary |
|---|---|
| `MaSpread_001_Rsi6040` | MA10/MA20 spread > 0.1% + RSI confirmation |
| `RsiReversion_6040` | Pure RSI: < 40 → CALL, > 60 → PUT |
| `MAAlignmentRoom_Fast` | MA5/MA10/MA20 stack aligned + resistance/support room |

---

## Adding a new strategy variant

### Step 1 — know what columns are available

The `df` passed to every signal function is the NIFTY feature store plus a
`regime` column. Key columns:

```
close_1515          rsi14               rsi5
ma10                ma20                ma50
bb_upper            bb_lower            bb_width
atr14               vix_close           vix_chg_pct
volume              ma20_slope          volatility_10d
resistance_distance_10d                 support_distance_10d
range_position_10d  ret_3d              ret_5d
regime              # "calm" or "stress" — same thresholds as production cascade
```

To see every column: `print(build_base().columns.tolist())`

### Step 2 — look up existing cascade signal keys (optional)

```python
oversold_bounce_call   # strategy_OversoldBounceCall_HighPrecision_signal
                       # strategy_OversoldBounceCall_MoreTrades_signal
                       # strategy_OversoldBounceCall_ContextRoom_signal

down_momentum_put      # strategy_DownMomentumPut_HighPrecision_signal
                       # strategy_DownMomentumPut_MoreTrades_signal
                       # strategy_DownMomentumPut_Fast_signal

momentum_directional   # strategy_MomentumDirectional_signal
                       # strategy_MomentumDirectional_ContextVotes_CallExpansionGuard_signal
                       # strategy_MomentumDirectional_ContextVotes_ExpansionGuard_signal
                       # strategy_MomentumDirectional_ContextVotes_StrongExpansionGuard_signal

mean_reversion         # strategy_BollingerMeanReversion_signal
                       # strategy_RsiMeanReversion_6040_signal

ma_alignment_room      # strategy_MAAlignmentRoom_PutGuarded_signal
                       # strategy_MAAlignmentRoom_ReboundCall_signal
                       # strategy_MaTrend_001_signal

range_breakout         # strategy_RangeBreakout_signal
                       # strategy_RangeBreakout_ATRBuffer_signal

calm_trend_call        # strategy_CalmTrendCall_Headroom_signal
                       # strategy_CalmTrendCall_Pullback_signal
                       # strategy_CalmTrendCall_ContextHeadroom_signal

calm_fade_put          # strategy_CalmFadePut_Overbought_signal
                       # strategy_CalmFadePut_ContextOverbought_signal

calm_momentum_put      # strategy_CalmMomentumPut_Continuation_signal
```

To inspect keys at runtime:
```python
from src.technical_analysis.cascade.dataset import build_base
from src.technical_analysis.cascade.strategies import momentum_directional
print(list(momentum_directional(build_base()).keys()))
```

---

### Option 1 — Parametric sweep of an existing strategy shape

Only file: [strategy_grid.py](strategy_grid.py) — add calls to `DEFAULT_VARIANTS`:

```python
rsi_reversion_variant("RsiReversion_3565", 35, 65),
ma_spread_variant("MaSpread_002_Rsi5545", 0.002, 55, 45),
```

---

### Option 2 — Tweak an existing cascade signal

Add to `PROMOTED_VARIANTS` or `EXPERIMENTAL_VARIANTS` in [strategy_grid.py](strategy_grid.py).
For calm/stress-specific signals, use `_gate_to_regime()`:

```python
# Layer a guard on a base signal
def _oversold_high_vix(df: pd.DataFrame) -> pd.Series:
    base = oversold_bounce_call(df)["strategy_OversoldBounceCall_ContextRoom_signal"]
    return _sig((base == CALL) & (df["vix_close"] >= 18), CALL)

StrategyVariant(
    name="OversoldBounce_HighVix18",
    signal_fn=_oversold_high_vix,
    description="OversoldBounce ContextRoom gated to VIX >= 18.",
),

# Gate a calm-only signal to the calm regime
StrategyVariant(
    name="CalmFadePut_Overbought",
    signal_fn=_gate_to_regime(
        lambda df: calm_fade_put(df)["strategy_CalmFadePut_Overbought_signal"],
        REGIME_CALM,
    ),
    description="PUT when rsi14 >= 65 and rsi5 >= 80. Calm regime only.",
),
```

If the tweak is permanent, also add it to:
- [src/technical_analysis/cascade/strategies.py](../../src/technical_analysis/cascade/strategies.py) — add the signal key to the family function

---

### Option 3 — Write a net-new signal from scratch

1. Write and register in [strategy_grid.py](strategy_grid.py) first:

```python
def _vix_spike_reversal(df: pd.DataFrame) -> pd.Series:
    vix_spike = df["vix_close"] / df["vix_close"].shift(1) - 1 >= 0.15
    rsi_room = df["rsi14"] <= 55
    return _sig(vix_spike & rsi_room, CALL)

StrategyVariant(
    name="VixSpikeReversal_15pct",
    signal_fn=_vix_spike_reversal,
    description="CALL when VIX spikes >=15% day-over-day and RSI14 <= 55.",
),
```

2. Once it shows edge in the grid, promote it to:
   - [src/technical_analysis/cascade/strategies.py](../../src/technical_analysis/cascade/strategies.py) — add as a proper family function
   - Register in the production pipeline in `src/technical_analysis/cascade/pipeline.py`

---

### Running in isolation

```powershell
# Test one variant before running the full grid
python -m backtest.vectorbt_research.strategy_grid --variants VixSpike --start 2025-06-01
```
