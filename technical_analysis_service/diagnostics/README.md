# Diagnostics and investigation tools

This folder holds ad hoc exports, prediction miss analysis and daily data-quality inspection. It is separate from strategy definitions and normal paper-trading workflows.

Run a tool with `python -m technical_analysis_service.diagnostics.<module> --help`. Its original command-line flags are retained. Results use the service's output configuration unless an explicit output path is supplied. Retired root script paths are no longer needed.

| Tool | Purpose |
| --- | --- |
| `analyze_precision_misses` | Precision/recall misses with surrounding feature context |
| `strategy_miss_audit` | All-session, all-active-strategy precision/recall audit with open/close moves, actual production-rule predicates, calendar-gap checks and Excel/CSV output |
| `streak_continuation_backtest` | Research-only continuation CALL: daily rule checks, nullable missing-gap decisions, label metrics, PUT conflict overlay and 15:10 underlying trade replay |
| `gap_up_policy_analysis` | Read-only paired comparison of retaining/removing CALL gap-up veto, half normal allocation and a half-standard cap; labels, walk-forward sensitivity and underlying return proxies |
| `daily_data_quality_check` | Missing/stale pre-decision inputs; exit 2 indicates a required input failure |
| `export_options_trading_log` | Option selection/execution, price excursions, labels and P&L |
| `export_production_strategy_metrics_log` | Per-date production strategy decisions and realized labels |

The data-quality check is suitable as an explicit preflight in the [operating runbook](../docs/operations.md). Other reports remain on demand; importing the app does not run them.

For offline per-strategy analysis:

```powershell
python -m technical_analysis_service.diagnostics.strategy_miss_audit --start 2026-01-01 --end 2026-11-09 --focus-start 2026-10-28 --output-dir output/analysis/nifty_2026_strategy_misses
```

Set `TECHNICAL_ANALYSIS_ENV_FILE` explicitly when using the root application's `.env`; see the operating runbook. The audit reads the database and writes only local reports. It combines the active production roster and research grid. It does not promote strategies or change production predictions.

`Strategy_day` includes misses and non-misses, missing-feature sessions and pending outcomes. Precision excludes ungraded labels; recall follows each strategy's declared direction. The audit also excludes outcome labels where consecutive available feature rows skip an exchange session. Original labels and exclusion reasons remain visible. Recomputed signals describe current rules and repaired data; stored production decisions are kept separately.

`export_production_strategy_metrics_log` remains the original wide export of features and production-strategy fires. `analyze_precision_misses` examines aggregate stored production predictions, with neighboring-session context; it is not a per-strategy evaluator.

For the StreakContinuationCall research report:

```powershell
python -m technical_analysis_service.diagnostics.streak_continuation_backtest --start 2024-01-01 --end 2026-11-09 --output-dir output/analysis/streak_continuation_call_streak_only_2026-11-09
```

See [strategy and replay conventions](../docs/streak_continuation_call.md). Missing required inputs remain ungraded, including gap values on non-setup rows. The research grid excludes those rows from precision/recall. The all-strategy miss audit also preserves nullable predictions instead of converting them into abstentions. Production decisions and the UptrendExhaustionPut rule are unchanged.

To compare the global CALL gap-up policy without changing it:

```powershell
python -m technical_analysis_service.diagnostics.gap_up_policy_analysis --start 2024-01-01 --end 2026-11-10 --output-dir output/analysis/gap_up_policy_comparison_2026-11-10
```

All variants retain event and PUT gap-down guards. Half-normal sizing multiplies a strategy's existing allocation by 0.5; for StreakContinuationCall this means 0.25 of standard allocation, versus 0.5 under the separate half-standard cap. Reported markouts are underlying-return units per fixed standard notional, not option P&L or compounded capital returns. The dedicated Streak trade proxy respects stop/target ordering and 15:10 exit; other strategies' live option-cascade returns are not invented. Full-source and calendar-verified cohorts are shown separately because local calendar coverage is incomplete.
