# Oracle Cloud VM Deployment

This guide configures an Oracle Cloud Always Free VM as the primary scheduler
for the NIFTY paper-trading pipeline. Job behavior and timing are defined in
[dailyRun.md](./dailyRun.md).

## Recommended Architecture

- Generate the daily Kite token on the trusted Windows machine.
- Persist the token to Supabase using the existing token script.
- Run market refresh, prediction, selection, paper entry, monitoring, and
  reporting on Oracle.
- Keep all durable state in Supabase.
- Retain Windows Task Scheduler as the fallback.

Keeping token generation local avoids Playwright/Chromium and TOTP handling on
an ARM server.

## Create the VM

1. Sign in to <https://cloud.oracle.com/>.
2. Choose the home region carefully. Prefer Mumbai or Hyderabad if Always Free
   capacity is available.
3. Open **Compute → Instances → Create instance**.
4. Name the instance `stockie-paper-trading`.
5. Select Ubuntu 22.04 or 24.04.
6. Select shape `VM.Standard.A1.Flex`.
7. Allocate 2 OCPUs and 12 GB RAM within the Always Free allowance.
8. Select or create a VCN with an Internet-connected public subnet.
9. Assign a public IPv4 address.
10. Add an SSH public key and create the instance.

Oracle's official guide is:
<https://docs.oracle.com/en-us/iaas/Content/Compute/tutorials/first-linux-instance/overview.htm>.

## SSH Key and Network Security

Generate a dedicated key on Windows:

```powershell
ssh-keygen -t ed25519 -C "stockie-oracle"
```

Upload only the `.pub` key. Keep the private key local.

Initially allow inbound TCP 22 only and restrict its source CIDR to the
administrator's public IP where practical. The scheduled jobs need outbound
Internet access but no public HTTP endpoint.

Do not expose Flask port 5000 directly. Use an SSH tunnel or a properly secured
HTTPS reverse proxy if remote dashboard access is later required.

Connect from Windows:

```powershell
ssh -i C:\path\to\private_key ubuntu@ORACLE_PUBLIC_IP
```

## Prepare Ubuntu

```bash
sudo timedatectl set-timezone Asia/Kolkata
timedatectl

sudo apt update
sudo apt upgrade -y
sudo apt install -y \
  git \
  python3 \
  python3-venv \
  python3-dev \
  build-essential \
  libpq-dev \
  unixodbc-dev \
  curl \
  jq
```

The ARM VM must successfully build or install every Python dependency. The
development packages above cover the expected PostgreSQL and ODBC build needs.

## Clone with a Read-Only Deploy Key

On the VM:

```bash
ssh-keygen -t ed25519 -C "stockie-oracle-deploy"
cat ~/.ssh/id_ed25519.pub
```

Add that public key under **GitHub repository → Settings → Deploy keys**.
Read-only access is sufficient.

```bash
git clone git@github.com:ckashyap23/Stockie26.git
cd Stockie26
```

Do not embed a personal access token in a clone URL.

## Python Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
```

Do not install Playwright Chromium when Kite login remains on Windows.

## Environment Configuration

Create `.env` in the repository root:

```bash
nano .env
```

Required baseline:

```dotenv
DATABASE_PROVIDER=supabase
SUPABASE_CONN_STR=postgresql://...

KITE_API_KEY=...
KITE_API_SECRET=...

TARGET_UNDERLYINGS=NIFTY,BANKNIFTY
TARGET_PCT_EFFECTIVE=0.006
SL_PCT_EFFECTIVE=0.004
NIFTY_TARGET_PCT=0.006
NIFTY_TARGET_PCT_RELAXED=0.004
MACRO_TRAILING_WINDOW=60
STRATEGY_FEATURE_CLEANUP_ENABLED=1
```

The VM does not need the Kite password or TOTP secret when it reads the access
token persisted by the Windows token job.

```bash
chmod 600 .env
mkdir -p logs
```

Never commit `.env`, access-token files, SSH private keys, or logs.

## Connectivity Test

After refreshing the Kite token on Windows:

```bash
source .venv/bin/activate

python scripts/daily_NIFTY/daily_optionInstrument_refresh.py \
  --type INDEX \
  --underlying NIFTY \
  --underlying BANKNIFTY \
  --dry-run
```

Then test the write:

```bash
python scripts/daily_NIFTY/daily_optionInstrument_refresh.py \
  --type INDEX \
  --underlying NIFTY \
  --underlying BANKNIFTY
```

Verify that failures return non-zero and that logs do not expose credentials.

## Cron Configuration

The VM timezone is IST, so cron expressions below use IST directly:

```bash
crontab -e
```

```cron
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin
MAILTO=""

0 3 * * 1-5 cd /home/ubuntu/Stockie26 && .venv/bin/python scripts/Common/load_daily_index_data.py --mode us-eur >> logs/us-eur.log 2>&1
30 8 * * 1-5 cd /home/ubuntu/Stockie26 && .venv/bin/python scripts/daily_NIFTY/daily_optionInstrument_refresh.py --type INDEX --underlying NIFTY --underlying BANKNIFTY >> logs/instruments.log 2>&1
0 9 * * 1-5 cd /home/ubuntu/Stockie26 && .venv/bin/python scripts/daily_NIFTY/daily_macro_refresh.py >> logs/macro.log 2>&1
15 9 * * 1-5 cd /home/ubuntu/Stockie26 && .venv/bin/python scripts/daily_NIFTY/daily_NIFTYoption_snapshot.py --underlying NIFTY --snapshot-label OPEN_0915 >> logs/option-snapshot.log 2>&1
20 9 * * 1-5 cd /home/ubuntu/Stockie26 && .venv/bin/python scripts/daily_NIFTY/daily_NIFTYGift_snapshot.py --mode open >> logs/gift-open.log 2>&1
21 9 * * 1-5 cd /home/ubuntu/Stockie26 && .venv/bin/python scripts/Common/load_daily_index_data.py --mode asia-partial >> logs/asia-partial.log 2>&1
22 9 * * 1-5 cd /home/ubuntu/Stockie26 && .venv/bin/python scripts/daily_NIFTY/daily_open_gap.py >> logs/open-gap.log 2>&1
24 9 * * 1-5 cd /home/ubuntu/Stockie26 && .venv/bin/python scripts/daily_NIFTY/daily_nifty_signal.py --underlying NIFTY --model-version cascade_v1 >> logs/signal.log 2>&1
27 9 * * 1-5 cd /home/ubuntu/Stockie26 && .venv/bin/python scripts/daily_NIFTY/daily_paper_entry.py --underlying NIFTY --model-version cascade_v1 --max-stale-seconds 300 >> logs/paper-entry.log 2>&1

30-59/5 9 * * 1-5 cd /home/ubuntu/Stockie26 && flock -n /tmp/stockie-monitor.lock .venv/bin/python scripts/daily_NIFTY/daily_paper_monitor.py --underlying NIFTY --model-version cascade_v1 --max-stale-seconds 300 >> logs/paper-monitor.log 2>&1
*/5 10-14 * * 1-5 cd /home/ubuntu/Stockie26 && flock -n /tmp/stockie-monitor.lock .venv/bin/python scripts/daily_NIFTY/daily_paper_monitor.py --underlying NIFTY --model-version cascade_v1 --max-stale-seconds 300 >> logs/paper-monitor.log 2>&1
0-25/5 15 * * 1-5 cd /home/ubuntu/Stockie26 && flock -n /tmp/stockie-monitor.lock .venv/bin/python scripts/daily_NIFTY/daily_paper_monitor.py --underlying NIFTY --model-version cascade_v1 --max-stale-seconds 300 >> logs/paper-monitor.log 2>&1
29 15 * * 1-5 cd /home/ubuntu/Stockie26 && flock -n /tmp/stockie-monitor.lock .venv/bin/python scripts/daily_NIFTY/daily_paper_monitor.py --underlying NIFTY --model-version cascade_v1 --max-stale-seconds 300 >> logs/paper-monitor.log 2>&1

15 15 * * 1-5 cd /home/ubuntu/Stockie26 && .venv/bin/python scripts/daily_NIFTY/daily_NIFTYGift_snapshot.py --mode close >> logs/gift-close.log 2>&1
32 15 * * 1-5 cd /home/ubuntu/Stockie26 && .venv/bin/python scripts/daily_NIFTY/daily_paper_report.py --underlying NIFTY --model-version cascade_v1 >> logs/paper-report.log 2>&1
45 15 * * 1-5 cd /home/ubuntu/Stockie26 && .venv/bin/python scripts/Common/load_daily_index_data.py --mode asia-final >> logs/asia-final.log 2>&1
47 15 * * 1-5 cd /home/ubuntu/Stockie26 && .venv/bin/python scripts/daily_NIFTY/daily_market_refresh.py --underlying NIFTY >> logs/market-refresh.log 2>&1
0 16 * * 1-5 cd /home/ubuntu/Stockie26 && .venv/bin/python scripts/daily_NIFTY/daily_NIFTYoption_OHLC.py --underlying NIFTY >> logs/option-ohlc.log 2>&1
10 16 * * 1-5 cd /home/ubuntu/Stockie26 && .venv/bin/python scripts/daily_NIFTY/daily_signal_summary.py --underlying NIFTY --model-version cascade_v1 >> logs/signal-summary.log 2>&1
```

Cron fires on weekday NSE holidays. Disable those dates or add a common
`TradingCalendar` guard before relying on unattended execution.

## Reliability and Security

- Use `flock` for monitor jobs so runs cannot overlap.
- Configure log rotation and disk-usage alerts.
- Alert on non-zero exits and missing daily heartbeat records.
- Keep the OS and Python dependencies patched.
- Keep SSH restricted and disable password authentication.
- Store all durable state in Supabase; treat the VM filesystem as replaceable.
- Do not expose the database connection string in process arguments or logs.
- Oracle may reclaim an Always Free VM it classifies as idle. Keep Windows Task
  Scheduler tested as the fallback.
- Recreate the VM from this repository and `.env`; do not depend on manual
  state that is not documented.
