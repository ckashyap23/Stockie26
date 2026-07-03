# Database Schema

Supabase is the durable source for the NIFTY pipeline.

## Core Tables

| Table | Contains |
|---|---|
| `WatchedInstrument` | Active instruments to track. |
| `TradingCalendar` | Valid NSE sessions and expiry flags. |
| `KiteAccessToken` | Latest Kite access token for cron jobs. |
| `UnderlyingSnapshot` | Daily underlying OHLCV. |
| `UnderlyingCandle5m` | Optional 5-minute underlying candles. |
| `SignalFeatureDaily` | Daily NIFTY technical features, including `atr7`, `atr7_sma`, `atr14`, and `atr14_sma`. |
| `MacroFactorDaily` | Macro factors, currently India VIX. |
| `GlobalIndexOhlc` | Global index OHLC for risk context. |

## Options

| Table | Contains |
|---|---|
| `OptionInstrument` | Active option contract master rows. |
| `OptionSnapshot` | Raw option quote/snapshot prices. |
| `OptionSnapshotCalc` | IV and Greeks from snapshots. |
| `OptionOhlc` | Daily-grain option OHLC rows. |

## Production

| Table | Contains |
|---|---|
| `NiftyPrediction` | Direction keyed by `signal_date`, with execution date, labels, strategy, regime, and global context. Includes realised quality columns (`bull_score`, `bear_score`, `signal_quality`, `actual_quality_label`, `quality_horizon_days`) that are populated on every prediction run — these are audit/grading fields and must never be used as same-day strategy inputs. |
| `NiftyOptionSelection` | Selected contract plus planned entry and target/stop percentages and prices. |

## News Sentiment

| Table | Contains |
|---|---|
| `NewsArticle` | Raw fetched articles. |
| `NewsArticleSentiment` | Per-article sentiment and sector weights. |
| `NiftyMarketSentiment` | Daily pre-market sentiment composite. |

## Paper Trading

| Table | Contains |
|---|---|
| `PaperExecutionSignal` | Option-selection row prepared for paper execution. |
| `PaperOrder` | Simulated fills, bid/ask context, and Kite charge details. |
| `PaperTradeResult` | Open/closed state, actual fills, gross P&L, charges, and net P&L. |
| `PaperTradeEvent` | Append-only paper lifecycle events. |

## Migrations

```powershell
Get-ChildItem src/data_manager/db/migrations
```

Most daily jobs defensively create/upgrade required tables through
`src/data_manager/db/supabase_client.py`, but migrations are the schema contract.
Migrations `001` through `015` are ordered history and must not be renamed,
squashed, or deleted after deployment.
