# Windows Task Scheduler

Windows Task Scheduler is the fallback scheduler for the forward-only NIFTY
paper-trading pipeline. The complete job sequence and timing contract are in
[dailyRun.md](./dailyRun.md).

## One-Time Preparation

Open PowerShell in the repository root:

```powershell
Set-Location "C:\Users\ckashyap\OneDrive - Microsoft\Github_Copilot\Stockie26"
Get-Command python
python --version
python -m pip install -r requirements.txt
python -m playwright install chromium
```

The scheduled tasks must use the same absolute Python executable returned by
`Get-Command python`. The current installation uses:

```text
C:\Python314\python.exe
```

Keep `.env` at the repository root and verify at least:

- `DATABASE_PROVIDER=supabase`
- `SUPABASE_CONN_STR`
- Kite API key and secret
- Kite user, password, and TOTP secret for automated token generation
- target, stop, label, and macro-window configuration

Never put credentials in Task Scheduler arguments.

## Kite Callback Port

`daily_get_kite_access_token.py` starts a callback server. Its default port
`5000` conflicts with the Flask dashboard.

Preferred configuration:

1. Register `http://127.0.0.1:5001/callback` as the redirect URL in the Zerodha
   developer application.
2. Set the following in `.env`:

   ```dotenv
   KITE_REDIRECT_HOST=127.0.0.1
   KITE_REDIRECT_PORT=5001
   ```

3. Test token generation manually:

   ```powershell
   python scripts\daily_NIFTY\daily_get_kite_access_token.py
   ```

If the redirect cannot be changed, stop Flask before token refresh and restart
it after the token has been stored.

## Creating a Task

For each job:

1. Open **Task Scheduler**.
2. Select **Task Scheduler Library**.
3. Select **Create Task** rather than **Create Basic Task**.
4. On **General**:
   - use a descriptive name such as `Stockie - Kite Token`;
   - select the Windows account that owns the repository;
   - select **Run whether user is logged on or not**;
   - use elevated privileges only when required.
5. On **Triggers**, add a weekly trigger for Monday-Friday at the required IST
   time.
6. On **Actions**, configure:

   ```text
   Program/script:
   C:\Python314\python.exe

   Add arguments:
   <script path and arguments>

   Start in:
   C:\Users\ckashyap\OneDrive - Microsoft\Github_Copilot\Stockie26
   ```

7. On **Conditions**:
   - select **Wake the computer to run this task**;
   - decide whether battery-powered execution is acceptable.
8. On **Settings**:
   - select **Run task as soon as possible after a scheduled start is missed**;
   - set an appropriate maximum runtime;
   - select **Do not start a new instance** when the task is already running.
9. Save the task and provide the Windows account password if requested.
10. Right-click the task, select **Run**, and verify its database/log outcome.

## Required Tasks

Use the following arguments with the common Python executable and working
directory above.

| Task | Time IST | Arguments |
|---|---:|---|
| Western indexes | 03:00 | `scripts\Common\load_daily_index_data.py --mode us-eur` |
| Kite token | 07:30 | `scripts\daily_NIFTY\daily_get_kite_access_token.py` |
| Option instruments | 08:30 | `scripts\daily_NIFTY\daily_optionInstrument_refresh.py --type INDEX --underlying NIFTY --underlying BANKNIFTY` |
| Macro refresh | 09:00 | `scripts\daily_NIFTY\daily_macro_refresh.py` |
| Option snapshot | 09:15 | `scripts\daily_NIFTY\daily_NIFTYoption_snapshot.py --underlying NIFTY --snapshot-label OPEN_0915` |
| GIFT open | 09:20 | `scripts\daily_NIFTY\daily_NIFTYGift_snapshot.py --mode open` |
| Asia partial | 09:21 | `scripts\Common\load_daily_index_data.py --mode asia-partial` |
| Open gap | 09:22 | `scripts\daily_NIFTY\daily_open_gap.py` |
| Prediction and selection | 09:24 | `scripts\daily_NIFTY\daily_nifty_signal.py --underlying NIFTY --model-version cascade_v1` |
| Paper entry | 09:27 | `scripts\daily_NIFTY\daily_paper_entry.py --underlying NIFTY --model-version cascade_v1 --max-stale-seconds 300` |
| Paper monitor | Every five minutes | `scripts\daily_NIFTY\daily_paper_monitor.py --underlying NIFTY --model-version cascade_v1 --max-stale-seconds 300` |
| GIFT close | 15:15 | `scripts\daily_NIFTY\daily_NIFTYGift_snapshot.py --mode close` |
| Final monitor | 15:29 | Same paper-monitor command |
| Paper report | 15:32 | `scripts\daily_NIFTY\daily_paper_report.py --underlying NIFTY --model-version cascade_v1` |
| Asia final | 15:45 | `scripts\Common\load_daily_index_data.py --mode asia-final` |
| Market refresh | 15:47 | `scripts\daily_NIFTY\daily_market_refresh.py --underlying NIFTY` |
| Option OHLC audit | 16:00 | `scripts\daily_NIFTY\daily_NIFTYoption_OHLC.py --underlying NIFTY` |
| Signal summary | 16:10 | `scripts\daily_NIFTY\daily_signal_summary.py --underlying NIFTY --model-version cascade_v1` |

## Five-Minute Monitor Triggers

Create one paper-monitor task with four triggers:

- 09:30-09:55 every five minutes;
- 10:00-14:55 every five minutes;
- 15:00-15:25 every five minutes;
- one final run at 15:29.

Configure **Do not start a new instance** so a slow quote/API call cannot create
overlapping monitor processes.

## Ordering and Failure Rules

- The 09:20-09:27 jobs are a dependency chain. Do not let a downstream task run
  after its prerequisite fails.
- Schedule `daily_nifty_signal.py`; do not also schedule the prediction and
  option-selection scripts separately.
- The 09:15 option snapshot must complete before the 09:20 option-selection
  as-of time.
- Run tasks only on NSE trading days. Standard weekly triggers also fire on NSE
  holidays, so disable those dates or add a shared trading-calendar guard.
- Keep the computer awake and connected throughout market hours.

## Logging and Verification

Task Scheduler history records starts and exit codes but not complete Python
output. For production fallback use, wrap each command in a PowerShell runner
that:

- appends stdout/stderr to a per-job rotating log;
- preserves the Python exit code;
- sends a failure notification;
- uses a lock for monitor jobs.

After initial setup, manually run and verify each task before relying on its
trigger.
