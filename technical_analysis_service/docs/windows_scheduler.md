# Windows Task Scheduler

## Repair activated on 9 November 2026

All 17 registered Stockie tasks now use the project virtual environment and scheduled_jobs runner, with Interactive logon for KASHYAP\kashy as explicitly chosen by the user. Locking the screen is supported; signing out is not. Wake settings and Scheduler Operational history are enabled, and battery-stop restrictions are removed. An actual one-shot clock trigger scheduled for 22:21:01 IST started at 22:21:01, returned code 0 and wrote PROBE_SUCCESS. The temporary verification task was removed afterward.

The original tasks used removed root scripts and system Python. Windows recorded sleep until 15:51 on 9 November followed by simultaneous catch-up launches around 15:57. Password-logon updates failed with 0x8007052e; unprivileged signed-in updates failed with Access Denied. The repair was applied using native Windows administrator approval. A zero-retry settings XML issue encountered during installation was corrected by omitting the optional restart block entirely. Final inventory verified 17/17 correct actions, principals and wake settings, zero battery-stop settings, and enabled history. There were no provider requests, broker logins or trades during the clock probe.

The repair script previews changes by default. It preserves the existing task times/account unless an explicit account option is supplied, backs up XML before each update, uses the virtual environment, enables wake/catch-up, removes battery-stop restrictions, prevents overlapping copies and sets bounded retries. GIFT Open receives a second 09:11 trigger for pre-open capture; the runner dispatches the correct phase. Existing weekday monitor triggers are retained, including 15:10.

For signed-in operation (screen locking is fine; signing out stops availability):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\Cursor_Github\OT_v1\technical_analysis_service\scripts\Common\repair_windows_tasks.ps1 -Apply -SignedIn
```

For operation while signed out, run locally and enter Windows credentials only into the local prompt. An administrator PowerShell is needed to enable scheduler history:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\Cursor_Github\OT_v1\technical_analysis_service\scripts\Common\repair_windows_tasks.ps1 -Apply -PromptForCredentials
```

The execution-policy option applies only to this process, not the machine policy. Do not paste credentials into chat, command arguments or a file. A Windows Hello PIN is not necessarily the account password required for stored task credentials.

The replacement action is the project Python with `-u -m technical_analysis_service.scheduled_jobs --workspace C:\Cursor_Github\OT_v1 --job <job-id>`. It explicitly selects the root .env/output paths. Logs are under `output/scheduler/logs/YYYY-MM-DD/`; latest states are under `output/scheduler/status/`. Each record distinguishes SUCCESS, FAILED, BLOCKED_DEPENDENCY, SKIPPED_HOLIDAY, SKIPPED_LATE and TIMEOUT. A zero Windows exit after an intentionally skipped late run does not mean a workflow executed.

```powershell
.venv/Scripts/python.exe -m technical_analysis_service.scheduled_jobs --workspace C:\Cursor_Github\OT_v1 --status
```

The runner refuses delayed morning entries after 09:45 IST and requires same-day upstream successes before prediction/entry. Scheduled monitors do not retry new entries after that window; paper.entry handles bounded morning retries. Existing-position monitoring has no upstream-data dependency. Database calendar errors fail closed. Special weekend exchange sessions still need separately configured times/triggers rather than blindly replaying the regular morning schedule.

Keep the laptop plugged in, connected and awake/able to wake through the trading window. Wake timers are enabled and AC idle sleep is already disabled; battery/lid/critical-power actions can still prevent execution. No local scheduler can execute while powered off. The repair does not silently disable battery or lid safety settings.

Kite login dependencies have been installed in the venv, matching the existing browser cache. Scheduled login uses headless mode; automatic login extracts the redirect request token without binding a local callback port, avoiding conflict with a running Flask app. Broker authentication, network availability and expired credentials remain external prerequisites and are reported as failures, not hidden as success.

## Original manual setup

Follow the job order, IST timings, exit policy and prerequisites in [operations.md](operations.md). These instructions configure the extracted service; they do not create or modify existing Windows tasks automatically.

Install the service into the Python environment used by the tasks. On this checkout the application interpreter is `C:\Cursor_Github\OT_v1\.venv\Scripts\python.exe`; use the actual interpreter path on another host.

Each task should use:

```text
Program/script: C:\Cursor_Github\OT_v1\.venv\Scripts\python.exe
Arguments:      -m technical_analysis_service run <task> -- <flags>
Start in:       C:\Cursor_Github\OT_v1
```

For example, the paper monitor arguments are:

```text
-m technical_analysis_service run paper.monitor -- --underlying NIFTY --model-version cascade_v1 --max-stale-seconds 300
```

Token refresh and the input-quality check use their module commands from the operations runbook instead of a task alias. Retain the existing US-close schedule's DST adjustment. The task account must have `TECHNICAL_ANALYSIS_ENV_FILE` and `TECHNICAL_ANALYSIS_OUTPUT_DIR` configured if reusing the root application's settings/output; otherwise the service's own `.env` and output defaults apply.

Configure a weekday trigger for each job and separately handle NSE holidays. For five-minute monitoring, use 09:30–09:55, 10:00–14:55 and 15:00–15:25 repeating triggers plus the 15:29 final run. Select **Do not start a new instance** for the monitor and any job whose previous run may still be active.

Use **Run whether user is logged on or not** when the account supports unattended operation, keep the machine awake during the operating window, and capture stdout/stderr and exit codes in a per-task log. Configure finite retries and failure alerts. A nonzero prerequisite exit must prevent its downstream jobs from running; independent time triggers alone do not enforce that dependency.

When updating existing task definitions, replace retired root script paths with these module/task arguments. Validate each changed definition in an appropriate non-production environment before relying on it. Secrets must not appear in arguments or logs. See the operations runbook for the callback-port conflict and the distinction between the root dashboard and standalone backend.
