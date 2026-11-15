# StreakContinuationCall

Status: SIGNAL, promoted to production at the user's request on 9 November 2026. It participates in the production cascade and feeds option selection and paper trading. It is removed from the default research grid; the standalone diagnostic and historical research workbooks remain available.

At D close, require a run of at least three sessions with close_1515 > open_915 AND ret_3d >= 1%. The breakout branch was removed at the user's request. Recent-high features are no longer required. A doji breaks the streak.

Require today's breadth_advance_decline_ratio >= 1.5 and its three-observation mean >= 1.5. Require close position within the day range >= 0.7 and trend_efficiency_5d >= 0.6. A zero/invalid day range is unknown. Known missing exchange-session boundaries reset the streak and invalidate breadth windows crossing the gap. Missing calendar periods are disclosed, not inferred.

At D+1 open, stored gap_open_atr must lie in [-0.5, 1.5], inclusive. Values outside that band veto. The available VIX guard is D close-to-close vix_chg_pct <= 0.10, in decimal units, evaluated with the next-open guard; it is not a measurement of D+1 opening VIX. Never shift in D+1 closing VIX. Missing gap or VIX values are ungraded. Other unknown required setup data is ungraded too. Nullable predictions preserve that distinction from NO_POSITION.

Entry is next open, without Strategy 2 confirmation. The research trade policy is target +0.6%, stop -0.4%, time exit 15:10 IST, size multiplier 0.5. The diagnostic uses underlying five-minute OHLC and the 15:10 candle's opening price for time exit. Bars must be continuous from 09:15 until exit. Missing bars, invalid bars or feature/candle opening-price mismatches are not scored as valid executions. Simultaneous stop/target touches in a five-minute bar produce return bounds. No first-touch order is invented. Half-size is a research exposure multiplier, not an instruction to submit a fractional exchange lot. No option premium P&L, fees or slippage is implied.

The existing full-session +0.6% touch label remains the precision/recall target. CALL/BOTH labels are CALL opportunities; a label hit does not mean the target was reached before a stop or the 15:10 time exit. Source-label metrics and a separate calendar-verified cohort are reported. Missing setup/guard observations are excluded from both precision and recall denominators, with ungraded opportunities counted separately.

On confirmed continuation CALLs, production signal gathering suppresses UptrendExhaustionPut before voting and primary-strategy selection. Missing/guard-vetoed CALLs do not suppress PUTs. The standalone diagnostic preserves the original PUT series alongside the suppressed copy. The user removed the global GAP_UP veto on 11 November 2026; the 4 November signal is consequently actionable under the current rules. This strategy's own [-0.5, 1.5] ATR opening-gap limits, the event guard and production PUT gap-down guard remain active. Sizing remains 0.5x standard allocation; the proposed extra half-size gap policy was not enabled.

Paper trading uses the existing next-session pipeline (currently prediction/selection around 09:24 and entry around 09:27), with the executable live ask and actual fill time. It cannot reproduce a 09:15 opening fill after later guards and option selection. Fixed underlying-move targets/stops are translated to option premium levels through the existing delta-adjusted execution helper: +0.6% target, -0.4% stop. On this strategy only, the target closes the position instead of ratcheting, and the monitor forces an exit at its first available quote at/after 15:10 IST, including when general EOD exits are disabled. No entry is permitted at/after 15:10 or on a different session date.

Sizing applies 0.5 to standard allocated premium capital and rounds down to whole option lots. Entry is rejected if the half allocation cannot fund one lot. Quantity is persisted on the paper signal before orders and OCOs are created. Paper signal preparation requires a matching actionable prediction/selection and carries this strategy's fixed target/stop instead of the general environment defaults. No real broker orders are introduced.

Reproduction (select the root .env explicitly for the current local app):

```powershell
$env:TECHNICAL_ANALYSIS_ENV_FILE = 'C:\Cursor_Github\OT_v1\.env'
$env:TECHNICAL_ANALYSIS_OUTPUT_DIR = 'C:\Cursor_Github\OT_v1\output'
.venv/Scripts/python.exe -m technical_analysis_service.diagnostics.streak_continuation_backtest --start 2024-01-01 --end 2026-11-09 --output-dir output/analysis/streak_continuation_call_streak_only_2026-11-09
```

The current streak-only replay evaluates 704 available feature sessions, producing 11 graded fires and five correct labels: precision 45.45%, recall 2.72%. For 2026 alone: four fires, three correct, precision 75%, recall 5.08%. The original streak-or-breakout report is preserved separately: 32 fires, six correct, precision 18.75%, recall 3.26%. The local calendar has no 2025 records; results for that year are source-label evidence only. Six feature dates in 2026 are missing. The small sample does not establish production readiness, particularly given the strategy was proposed after observing the November rally.
