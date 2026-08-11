"""
Daily signal summary: prints prediction direction and selected option instrument.

Reads NiftyPrediction (effective_prediction) and NiftyOptionSelection
(primary_buy_symbol) for the given execution date and outputs a one-line summary
suitable for cron logging or downstream piping.

Usage:
    python scripts/daily_NIFTY/daily_signal_summary.py
    python scripts/daily_NIFTY/daily_signal_summary.py --trade-date 2026-08-11
    python scripts/daily_NIFTY/daily_signal_summary.py --underlying NIFTY --model-version cascade_v1
    python scripts/daily_NIFTY/daily_signal_summary.py --json
    python scripts/daily_NIFTY/daily_signal_summary.py --email
    python scripts/daily_NIFTY/daily_signal_summary.py --json --email

Email env vars (same as rest of project):
    NOTIFY_EMAIL_FROM      Gmail address to send from
    NOTIFY_EMAIL_PASSWORD  Gmail App Password
    NOTIFY_EMAIL_TO        Space or comma-separated recipient list
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
import sys
from datetime import date, datetime
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))
load_dotenv(project_root / ".env")

from src.common.config import get_settings
from src.data_manager.db.client_factory import get_database_client

# ── Email config (shared with daily_mail_notification.py) ─────────────────────
_NOTIFY_FROM     = (os.getenv("NOTIFY_EMAIL_FROM") or "").strip()
_NOTIFY_PASSWORD = (os.getenv("NOTIFY_EMAIL_PASSWORD") or "").strip()
_to_raw          = os.getenv("NOTIFY_EMAIL_TO", "")
_NOTIFY_TO       = [a.strip() for a in _to_raw.replace(",", " ").split() if a.strip()]
_SMTP_HOST       = os.getenv("NOTIFY_SMTP_HOST", "smtp.gmail.com")
_SMTP_PORT       = int(os.getenv("NOTIFY_SMTP_PORT", "587"))
_SMTP_SSL_PORT   = 465
_SMTP_TIMEOUT    = 20


def _default_trade_date() -> date:
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


# ── Email helpers ──────────────────────────────────────────────────────────────

def _build_email_body(result: dict) -> tuple[str, str]:
    """Return (subject, body) for the signal summary result."""
    direction = result.get("direction") or "NO_POSITION"
    symbol = result.get("underlying", "NIFTY")
    trade_date = result.get("trade_date", "")

    if direction == "NO_POSITION":
        subject = f"Stockie Signal {trade_date} — {symbol} NO_POSITION"
        reason = result.get("no_trade_reason") or result.get("reason") or ""
        body = (
            f"Date: {trade_date}\n"
            f"Underlying: {symbol}\n"
            f"Direction: NO_POSITION\n"
            + (f"Reason: {reason}\n" if reason else "")
        )
    else:
        instrument = result.get("option_instrument") or "—"
        drift = result.get("drift_effective_prediction")
        base = result.get("base_prediction")
        drift_reason = result.get("drift_overrule_reason") or ""
        subject = f"Stockie Signal {trade_date} — {symbol} {direction} → {instrument}"
        body = (
            f"Date: {trade_date}\n"
            f"Signal Date: {result.get('signal_date', '')}\n"
            f"Underlying: {symbol}\n"
            f"Direction: {direction}\n"
            + (f"Base Prediction: {base}  (drift override → {drift})\n" if drift and drift != base else f"Base Prediction: {base}\n")
            + (f"Drift Reason: {drift_reason}\n" if drift_reason else "")
            + f"Strategy: {result.get('selected_strategy') or '—'}\n"
            f"\n"
            f"Option Instrument: {instrument}\n"
            f"Strike: {result.get('strike') or '—'}\n"
            f"Expiry: {result.get('expiry') or '—'}\n"
            f"Option Type: {result.get('option_type') or '—'}\n"
            f"Entry Ref Price: {result.get('entry_ref_price') or '—'}\n"
            f"\n"
            f"Strength Score: {result.get('strength_score') or '—'}\n"
            f"Selection Score: {result.get('selection_score') or '—'}\n"
            f"Volatility Regime: {result.get('volatility_regime') or '—'}\n"
        )
    return subject, body


def _send_email(subject: str, body: str) -> None:
    if not _NOTIFY_FROM or not _NOTIFY_PASSWORD or not _NOTIFY_TO:
        print(
            "Email not configured — set NOTIFY_EMAIL_FROM, NOTIFY_EMAIL_PASSWORD, "
            "NOTIFY_EMAIL_TO in .env",
            file=sys.stderr,
        )
        return

    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = _NOTIFY_FROM
    msg["To"] = ", ".join(_NOTIFY_TO)

    ctx = ssl.create_default_context()
    try:
        with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT, timeout=_SMTP_TIMEOUT) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ctx)
            smtp.ehlo()
            smtp.login(_NOTIFY_FROM, _NOTIFY_PASSWORD)
            smtp.sendmail(_NOTIFY_FROM, _NOTIFY_TO, msg.as_string())
    except Exception:
        # Fallback to SSL
        with smtplib.SMTP_SSL(_SMTP_HOST, _SMTP_SSL_PORT, timeout=_SMTP_TIMEOUT, context=ctx) as smtp:
            smtp.login(_NOTIFY_FROM, _NOTIFY_PASSWORD)
            smtp.sendmail(_NOTIFY_FROM, _NOTIFY_TO, msg.as_string())

    print(f"Email sent to {', '.join(_NOTIFY_TO)}: {subject}")


def fetch_signal_summary(
    conn,
    symbol: str,
    model_version: str,
    trade_date: date,
) -> dict:
    """
    Join NiftyPrediction + NiftyOptionSelection for the given execution date.

    NiftyPrediction.next_trade_date == trade_date (execution/paper-trade date).
    NiftyOptionSelection.next_trade_date == trade_date (same key).
    """
    sql = """
        SELECT
            p.signal_date,
            p.next_trade_date,
            -- drift_effective_prediction overrides effective_prediction when set
            -- (e.g. NO_POSITION base signal promoted to PUT via drift probe).
            -- Fall back to NiftyOptionSelection.prediction_direction which also
            -- reflects the drift override used at selection time.
            COALESCE(
                p.drift_effective_prediction,
                o.prediction_direction,
                p.effective_prediction,
                'NO_POSITION'
            ) AS direction,
            p.effective_prediction      AS base_prediction,
            p.drift_effective_prediction,
            p.drift_overrule_reason,
            p.strength_score,
            p.volatility_regime,
            o.selected_strategy,
            o.primary_buy_symbol  AS option_instrument,
            o.primary_buy_strike  AS strike,
            o.primary_buy_expiry  AS expiry,
            o.primary_buy_option_type AS option_type,
            o.primary_buy_entry_price AS entry_ref_price,
            o.selection_score,
            o.no_trade_reason
        FROM "NiftyPrediction" p
        LEFT JOIN "NiftyOptionSelection" o
               ON o.symbol        = p.symbol
              AND o.model_version = p.model_version
              AND o.trade_date    = p.signal_date
        WHERE UPPER(p.symbol)       = %s
          AND p.model_version       = %s
          AND p.next_trade_date     = %s
        ORDER BY p.signal_date DESC
        LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(sql, (symbol.upper(), model_version, trade_date))
        row = cur.fetchone()
        cols = [d[0] for d in cur.description] if cur.description else []
    if row is None:
        return {}
    return dict(zip(cols, row, strict=False))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Print today's prediction direction and option instrument."
    )
    parser.add_argument("--trade-date", default=None, help="Execution date YYYY-MM-DD. Default: today IST")
    parser.add_argument("--underlying", default="NIFTY", help="Underlying symbol. Default: NIFTY")
    parser.add_argument("--model-version", default="cascade_v1", help="Model version. Default: cascade_v1")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Output as JSON instead of plain text")
    parser.add_argument("--email", action="store_true", help="Send summary email via NOTIFY_EMAIL_* env vars")
    args = parser.parse_args()

    trade_date = date.fromisoformat(args.trade_date) if args.trade_date else _default_trade_date()
    symbol = args.underlying.upper()

    settings = get_settings()
    db = get_database_client(settings)
    db.connect()
    try:
        summary = fetch_signal_summary(db.conn, symbol, args.model_version, trade_date)
    finally:
        db.close()

    if not summary:
        result = {
            "trade_date": trade_date.isoformat(),
            "underlying": symbol,
            "direction": "NO_POSITION",
            "option_instrument": None,
            "reason": "No NiftyPrediction row found for this execution date",
        }
    else:
        result = {
            "trade_date": trade_date.isoformat(),
            "signal_date": str(summary.get("signal_date") or ""),
            "underlying": symbol,
            "direction": summary.get("direction") or "NO_POSITION",
            "base_prediction": summary.get("base_prediction"),
            "drift_effective_prediction": summary.get("drift_effective_prediction"),
            "drift_overrule_reason": summary.get("drift_overrule_reason"),
            "strength_score": summary.get("strength_score"),
            "volatility_regime": summary.get("volatility_regime"),
            "selected_strategy": summary.get("selected_strategy"),
            "option_instrument": summary.get("option_instrument"),
            "strike": summary.get("strike"),
            "expiry": str(summary.get("expiry") or ""),
            "option_type": summary.get("option_type"),
            "entry_ref_price": summary.get("entry_ref_price"),
            "selection_score": summary.get("selection_score"),
            "no_trade_reason": summary.get("no_trade_reason"),
        }

    if args.as_json:
        print(json.dumps(result, default=str))
    else:
        direction = result["direction"]
        instrument = result.get("option_instrument") or "—"
        reason = result.get("no_trade_reason") or ""
        print(
            f"{trade_date}  {symbol}  direction={direction}"
            + (f"  instrument={instrument}" if direction != "NO_POSITION" else f"  no_trade_reason={reason}")
        )
        if direction != "NO_POSITION" and result.get("strike"):
            print(
                f"  strike={result['strike']}  expiry={result['expiry']}"
                f"  entry_ref={result['entry_ref_price']}  score={result['selection_score']}"
            )

    if args.email:
        subject, body = _build_email_body(result)
        _send_email(subject, body)


if __name__ == "__main__":
    main()
