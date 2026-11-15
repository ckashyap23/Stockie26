# Linux VM scheduling

Use the same [operating contract](operations.md) on a Linux host. Provider-specific free-tier claims and old machine paths have been removed; provision a host with suitable Python, network access and persistent storage according to your own hosting requirements.

Install Python 3.11 or newer and a virtual environment, then install the extracted service:

```bash
python3 -m venv /srv/stockie/.venv
/srv/stockie/.venv/bin/python -m pip install /srv/stockie/technical_analysis_service
sudo timedatectl set-timezone Asia/Kolkata
```

Store application secrets in a restricted environment file and use an absolute persistent output path. Supply these variables to cron/systemd, for example:

```text
TECHNICAL_ANALYSIS_ENV_FILE=/srv/stockie/technical.env
TECHNICAL_ANALYSIS_OUTPUT_DIR=/srv/stockie/output
```

For cron, set the scheduler/host timezone to IST and use absolute interpreter paths. An example monitor invocation, using `flock` on a host where it is installed, is:

```cron
30-59/5 9 * * 1-5 flock -n /srv/stockie/paper-monitor.lock /srv/stockie/.venv/bin/python -m technical_analysis_service run paper.monitor -- --underlying NIFTY --model-version cascade_v1 --max-stale-seconds 300 >> /srv/stockie/paper-monitor.log 2>&1
```

Add the remaining monitor windows and daily jobs using the operations runbook. Weekday cron entries also fire on NSE holidays; add a trading-calendar gate. Use a shared lock for every monitor trigger, enforce upstream success before downstream tasks, rotate logs and alert on failures. Coordinate API and dashboard jobs with external writers.

Kite token generation may stay on the trusted local machine if the database-backed token is shared. If moving token generation to Linux, install the `broker-login` extra and Chromium and validate the broker callback/login setup there. ODBC is needed only for the optional Azure SQL adapter; Supabase does not need the retired root ODBC build script.

Restrict administrative access, keep dependencies patched and protect database credentials. Expose the API only through your authenticated HTTPS setup, or use an SSH tunnel for private dashboard access. Persistent reports/job status need backed-up storage; durable trading records remain in the configured database. This guide does not deploy a VM or edit a live crontab.
