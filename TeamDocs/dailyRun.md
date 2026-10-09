# Daily NIFTY Paper-Trading Runbook

This runbook defines the forward-only production and paper-trading schedule.
Research Backtest and Production Backtest do not use option execution data.

Deployment guides:

- [Windows Task Scheduler](./taskScheduler.md)
- [Oracle Cloud VM](./OracleCloudVM.md)

## Trading-Date Semantics

For trade date `D`, the prediction is stored against the preceding trading
session `D-1`:

- `NiftyPrediction.signal_date = D-1`
- `NiftyPrediction.next_trade_date = D`
- completed market, technical, macro, PCR, OI, and VIX features come from
  `D-1`;
- US/European, Asian, GIFT NIFTY, and NIFTY open-gap context captured on the
  morning of `D` is incorporated before the prediction is finalized;
- the selected option is entered and monitored on `D`.

For Monday, 12 October 2026, the signal date is Friday, 9 October 2026 and the
next trade date is 12 October 2026.

## Required Schedule

Run these jobs only on NSE trading days unless the job is explicitly needed to
prepare the following session. All times are Asia/Kolkata (IST).

| Time | Purpose | Command |
|---|---|---|
| 03:00 | Load completed D-1 US and European index bars | `python scripts/Common/load_daily_index_data.py --mode us-eur` |
| 07:30 | Generate or validate the daily Kite access token | `python scripts/daily_NIFTY/daily_get_kite_access_token.py` |
| 08:30 | Refresh all active NIFTY and BANKNIFTY option contracts | `python scripts/daily_NIFTY/daily_optionInstrument_refresh.py --type INDEX --underlying NIFTY --underlying BANKNIFTY` |
| 09:00 | Refresh D-1 macro, VIX, FII/DII, and participant-OI inputs | `python scripts/daily_NIFTY/daily_macro_refresh.py` |
| 09:15 | Capture the NIFTY option chain and calculate IV/Greeks for selection | `python scripts/daily_NIFTY/daily_NIFTYoption_snapshot.py --underlying NIFTY --snapshot-label OPEN_0915` |
| 09:20 | Capture the opening GIFT NIFTY reference | `python scripts/daily_NIFTY/daily_NIFTYGift_snapshot.py --mode open` |
| 09:21 | Load Asian partial-session index bars through approximately 09:20 | `python scripts/Common/load_daily_index_data.py --mode asia-partial` |
| 09:22 | Save the completed NIFTY 09:15 candle and calculate GIFT/open-gap features | `python scripts/daily_NIFTY/daily_open_gap.py` |
| 09:24 | Generate the prediction for D and select the NIFTY option | `python scripts/daily_NIFTY/daily_nifty_signal.py --underlying NIFTY --model-version cascade_v1` |
| 09:27 | Prepare the paper signal, simulate the limit fill, and create the target/stop OCO | `python scripts/daily_NIFTY/daily_paper_entry.py --underlying NIFTY --model-version cascade_v1 --max-stale-seconds 300` |
| Every 5 minutes, 09:30-15:25 | Mark open trades, evaluate target/stop, and advance the anchored target cascade | `python scripts/daily_NIFTY/daily_paper_monitor.py --underlying NIFTY --model-version cascade_v1 --max-stale-seconds 300` |
| 15:15 | Capture the closing GIFT NIFTY reference for the next session | `python scripts/daily_NIFTY/daily_NIFTYGift_snapshot.py --mode close` |
| 15:29 | Perform the final near-close option quote and MTM update | `python scripts/daily_NIFTY/daily_paper_monitor.py --underlying NIFTY --model-version cascade_v1 --max-stale-seconds 300` |
| 15:32 | Write realized and open-position paper PnL reports | `python scripts/daily_NIFTY/daily_paper_report.py --underlying NIFTY --model-version cascade_v1` |
| 15:45 | Load completed Asian index bars for the next session's overnight features | `python scripts/Common/load_daily_index_data.py --mode asia-final` |
| 15:47 | Refresh NIFTY daily/5-minute data, labels, and D-day features for the next session | `python scripts/daily_NIFTY/daily_market_refresh.py --underlying NIFTY` |
| 16:00 | Persist NIFTY option daily OHLC for forward execution auditing | `python scripts/daily_NIFTY/daily_NIFTYoption_OHLC.py --underlying NIFTY` |
| 16:10 | Optionally write/send the daily signal summary | `python scripts/daily_NIFTY/daily_signal_summary.py --underlying NIFTY --model-version cascade_v1` |

The option instrument refresh makes both NIFTY and BANKNIFTY contracts
available. The prediction, selection, and paper-execution commands currently
trade only NIFTY.

## Orchestration Rules

1. Run the 09:20-09:27 jobs sequentially, not concurrently. A failure in an
   upstream job must stop the chain and raise an alert.
2. Schedule `daily_nifty_signal.py`, not separate prediction and option-selection
   jobs. It already invokes `daily_nifty_prediction.py` followed by
   `daily_option_selection.py`.
3. Capture the option-chain snapshot at 09:15. Option selection uses a 09:20
   as-of timestamp, so a snapshot recorded after 09:20 is not eligible.
4. `daily_paper_entry.py` obtains a fresh quote for the simulated marketable
   limit fill. It records the NIFTY level and option delta, then calculates the
   initial delta-adjusted target and stop.
5. A target touch is a virtual trigger, not an exit. It cancels the sibling stop
   and creates the next OCO pair using the initial target/stop distances. A stop
   touch closes the paper trade.
6. `daily_paper_monitor.py` retries any still-planned entry by default. Use
   `--skip-pending-entry` only when entry retry is intentionally disabled.
7. Do not overlap monitor processes. Configure the scheduler to skip a new run
   when the previous run is still active.

## Paper PnL

`daily_paper_report.py` reads `PaperTradeResult` and reports:

- closed positions using realized entry-to-exit PnL;
- open positions using entry-to-the-last-monitored executable bid;
- gross PnL, estimated Kite charges, and net PnL.

The 15:29 monitor provides the final near-close mark. It is an executable-bid
MTM and is not currently a separately sourced official exchange closing price.
Obtaining an official close would require an additional EOD marking step.

## Kite Token Port

The token script starts a local callback server. Its default
`KITE_REDIRECT_PORT` is `5000`, which conflicts with the Flask dashboard when
Flask is listening on the same port.

For unattended use, configure another callback port and register the exact same
redirect URL in the Zerodha developer application. Otherwise, stop Flask before
token refresh and restart it after the token has been persisted.

The token script can perform headless login when the required Kite user,
password, and TOTP environment variables are configured. Never commit those
values or the generated access token to Git.

## Recommended Free Scheduler

### Primary: Windows Task Scheduler on the existing machine

This is the recommended zero-cost setup for this repository:

- no hosted-runner startup or queue delay;
- no monthly invocation/minute quota;
- straightforward five-minute monitoring;
- Kite Playwright/TOTP login remains on the trusted machine;
- the current Windows/Python environment can be reused;
- secrets remain in the local `.env`.

The machine must remain awake, connected to the internet, and configured not to
sleep during market hours. Each task should:

- use the repository root as its working directory;
- use the absolute path to the intended Python executable;
- write stdout/stderr to rotating logs;
- fail visibly on a non-zero exit code;
- prevent a second instance when one is already running;
- retry transient startup failures without retrying indefinitely.

For the monitor, create three five-minute triggers plus the final mark:

- 09:30-09:55 every five minutes;
- 10:00-14:55 every five minutes;
- 15:00-15:25 every five minutes;
- one final run at 15:29.

### Cloud fallback: Oracle Cloud Always Free VM

Oracle documents Always Free AMD micro instances and Ampere A1 capacity. A
Linux VM can run native cron or systemd timers without an external scheduler:

<https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm>

Important limitations:

- Always Free capacity may be unavailable in the selected home region;
- Oracle may reclaim an instance it classifies as idle;
- OS patching, firewall rules, monitoring, and secret management are the
  operator's responsibility;
- Playwright/Kite login must be tested for the chosen CPU architecture;
- the VM timezone should be set to `Asia/Kolkata`.

A practical hybrid is to refresh the Kite token on the local Windows machine,
which already persists it to the database, and run the remaining jobs on the
VM.

## Other Free Services

- **GitHub Actions:** standard runners are free for public repositories, while
  private repositories consume the account's included minutes. Scheduled jobs
  can be queued or delayed, and approximately 1,700 monthly five-minute monitor
  invocations create significant startup overhead. Self-hosted runners avoid
  runner-minute charges but do not eliminate scheduled-event lag. See
  <https://docs.github.com/en/billing/concepts/product-billing/github-actions>.
- **Google Cloud:** an eligible always-free Compute Engine VM can use native
  cron, but the small free VM is resource-constrained and requires a billing
  account. Validate region, egress, public-IP, and current free-tier limits
  before deployment. See
  <https://cloud.google.com/free/docs/free-cloud-features#compute>.
- **PythonAnywhere free:** not suitable. Current free-account capabilities do
  not provide frequent external-API scheduled jobs. See
  <https://help.pythonanywhere.com/pages/FreeAccountsFeatures/>.
- **cron-job.org:** only sends HTTP requests; it does not execute this Python
  repository. It would still require a secure, always-available hosted endpoint,
  so it does not remove the hosting requirement. See <https://cron-job.org/>.

## Recommended Deployment Decision

Use Windows Task Scheduler for the initial paper-trading period beginning
12 October 2026. It is the lowest-risk and lowest-latency free option. After the
workflow has run reliably for several weeks, Oracle Always Free can be evaluated
as a secondary scheduler or failover host.
