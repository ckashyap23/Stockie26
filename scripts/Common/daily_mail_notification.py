"""
Daily Kite token health check — sends an email alert if the access token
in the DB was not refreshed today.

Schedule AFTER daily_get_kite_access_token.py, e.g. 09:00 IST.

Required env vars:
    NOTIFY_EMAIL_FROM      Gmail address used to send (e.g. bot@gmail.com)
    NOTIFY_EMAIL_PASSWORD  Gmail App Password (not your login password)
    NOTIFY_EMAIL_TO        Recipient address (can be the same as FROM)

Optional:
    NOTIFY_SMTP_HOST       Default: smtp.gmail.com
    NOTIFY_SMTP_PORT       Default: 587
"""
from __future__ import annotations

import os
import smtplib
import sys
from datetime import date, timezone
from email.mime.text import MIMEText
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from dotenv import load_dotenv
load_dotenv(_repo_root / ".env")

NOTIFY_FROM     = os.getenv("NOTIFY_EMAIL_FROM", "")
NOTIFY_PASSWORD = os.getenv("NOTIFY_EMAIL_PASSWORD", "")
NOTIFY_TO       = os.getenv("NOTIFY_EMAIL_TO", "")
SMTP_HOST       = os.getenv("NOTIFY_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT       = int(os.getenv("NOTIFY_SMTP_PORT", "587"))


def _fetch_token_updated_at() -> tuple[str | None, date | None]:
    """Return (masked_token, updated_at_date) from DB, or (None, None) on error."""
    from src.common.config import get_settings
    from src.data_manager.db.client_factory import get_database_client

    settings = get_settings()
    db = get_database_client(settings)
    db.connect()
    try:
        with db.conn.cursor() as cur:
            cur.execute(
                'SELECT access_token, updated_at FROM "KiteAccessToken" '
                "ORDER BY updated_at DESC LIMIT 1"
            )
            row = cur.fetchone()
    finally:
        db.close()

    if not row or not row[0]:
        return None, None

    token: str = str(row[0]).strip()
    masked = token[:6] + "…" + token[-4:] if len(token) > 10 else "***"
    updated_at_utc = row[1]
    if updated_at_utc is None:
        return masked, None

    updated_date = updated_at_utc.astimezone(timezone.utc).date()
    return masked, updated_date


def _send_email(subject: str, body: str) -> None:
    if not NOTIFY_FROM or not NOTIFY_PASSWORD or not NOTIFY_TO:
        print("Email not configured. Set NOTIFY_EMAIL_FROM / NOTIFY_EMAIL_PASSWORD / NOTIFY_EMAIL_TO.")
        return

    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"]    = NOTIFY_FROM
    msg["To"]      = NOTIFY_TO

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(NOTIFY_FROM, NOTIFY_PASSWORD)
        server.sendmail(NOTIFY_FROM, [NOTIFY_TO], msg.as_string())
    print(f"Email sent to {NOTIFY_TO}: {subject}")


def main() -> None:
    today = date.today()
    print(f"Checking Kite token freshness for {today} …")

    masked, updated_date = _fetch_token_updated_at()

    if masked is None:
        subject = "⚠️ Stockie Alert: No Kite access token in DB"
        body = (
            f"Date: {today}\n\n"
            "No Kite access token was found in the database.\n"
            "The daily_get_kite_access_token.py script may not have run or failed silently.\n\n"
            "Action required: check Render cron logs and re-run the token script manually."
        )
        print("ALERT: No token found in DB.")
        _send_email(subject, body)
        sys.exit(1)

    if updated_date != today:
        subject = f"⚠️ Stockie Alert: Kite token not refreshed today ({today})"
        body = (
            f"Date: {today}\n\n"
            f"The Kite access token in the database was last updated on {updated_date}, "
            f"not today.\n"
            f"Token (masked): {masked}\n\n"
            "The daily_get_kite_access_token.py cron likely failed this morning.\n"
            "Action required: check Render cron logs and re-run the token script manually."
        )
        print(f"ALERT: Token last updated {updated_date}, expected {today}.")
        _send_email(subject, body)
        sys.exit(1)

    print(f"OK: Token refreshed today ({updated_date}). Token (masked): {masked}. No alert sent.")


if __name__ == "__main__":
    main()
