# Run Backtesting

Backtesting is split into production replay, research strategy evaluation, and
executed-trade replay.

## Production Replay

Production replay has three steps:

1. Rebuild/upsert `NiftyPrediction`.
2. Upsert historical `NiftyOptionSelection`.
3. Replay option PnL.

The UI **Recompute Predictions** action runs the same production date range
through all three steps. The default date range is `2024-01-01` through today.

## Research Grid

The research grid evaluates raw strategy variants with ITM-delta option replay.
It is useful for comparing edge, but it does not change production authority.
Production promotion/demotion is manual through `strategy_families.yaml`.

## Executed Trades

Executed-trade replay reads actual paper/live fills and reports portfolio-level
performance. It does not simulate new entries from predictions.

## Miss Analysis

**Analyze Misses** exports precision and recall miss CSVs. Each miss includes the
miss signal date plus available D-2/D-1/D/D+1/D+2 signal-feature context.
