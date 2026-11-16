"""Morning email: trade date, NIFTY prediction and overall news score in the subject; option and paper-trade details in the body;
the news service's article-level scores (every article, its Gemini score and its seed) attached as a CSV.

Runs as the `morning-email` job (09:32 IST, after prediction at 09:24 and paper entry at 09:27). It never fails the morning on missing
pieces: a missing news score, paper trade or attachment is reported inside the email instead.

    python -m technical_analysis_service.scripts.daily_NIFTY.daily_morning_email [--trade-date YYYY-MM-DD] [--dry-run]

Environment: EMAIL_PROVIDER, RESEND_API_KEY, EMAIL_FROM, NOTIFY_EMAIL_TO (see common/mailer.py); NEWS_SERVICE_URL and NEWS_API_KEY
(the news service's base URL and one of its API_KEYS) for the attachment. On Railway use the private address, for example
http://<news-service-name>.railway.internal:8080.
"""
from __future__ import annotations

from technical_analysis_service.runtime import load_environment

import argparse
import html
import os
import sys
from datetime import date

import requests

load_environment()

from technical_analysis_service.common import mailer
from technical_analysis_service.common.config import get_settings
from technical_analysis_service.data_manager.db.client_factory import get_database_client
from technical_analysis_service.scripts.Common.schedule_guard import skip_if_weekend
from technical_analysis_service.scripts.daily_NIFTY.daily_signal_summary import _default_trade_date, fetch_signal_summary

TRADE_DIRECTIONS = {"CALL", "PUT"}


def _num(value, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def _signed(value) -> str:
    try:
        return f"{float(value):+.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _pct(value) -> str:
    try:
        return f"{float(value) * 100:.2f}% NIFTY move"
    except (TypeError, ValueError):
        return "-"


def build_message(
    trade_date: date,
    symbol: str,
    summary: dict,
    trades: list[dict],
    attachment_note: str,
) -> tuple[str, str, str]:
    """Return (subject, plain text, html). Pure function: all data comes in as arguments."""
    direction = (summary.get("direction") or "").upper() if summary else ""
    if not summary:
        label = "NO PREDICTION"
    elif direction in TRADE_DIRECTIONS:
        label = direction
    else:
        label = "NO TRADE"
    news = summary.get("news_score") if summary else None
    news_text = _signed(news) + (f" ({summary['news_direction']})" if summary and news is not None and summary.get("news_direction") else "")
    subject = f"Stockie {trade_date.isoformat()} | {symbol} {label} | News {_signed(news)}"

    lines = [f"{symbol}  trade date {trade_date.isoformat()}"]
    if not summary:
        lines += ["", "No prediction row exists for this trade date yet. Check the prediction job."]
    else:
        lines.append(f"Signal date: {summary.get('signal_date') or '-'}")
        lines.append(f"Prediction: {label}" + (f"  (strategy: {summary['selected_strategy']})" if summary.get("selected_strategy") else ""))
        confidence = summary.get("news_confidence")
        lines.append(f"News score: {news_text}" + (f", confidence {_num(confidence)}" if confidence is not None else ""))
        if news is None:
            lines.append("  (the 08:30 news run has not published a score for this date)")
        lines.append(f"Signal strength: {_num(summary.get('strength_score'))}")
        if direction in TRADE_DIRECTIONS:
            lines += [
                "",
                "Selected option",
                f"  Instrument: {summary.get('option_instrument') or '-'}",
                f"  Type / strike / expiry: {summary.get('option_type') or '-'} / {summary.get('strike') or '-'} / {summary.get('expiry') or '-'}",
                f"  Reference entry price: {_num(summary.get('entry_ref_price'))}",
            ]
            if not trades:
                lines += ["", "Paper trade: none found yet (paper entry may have failed or still be pending)."]
            for trade in trades:
                unit_price = trade.get("entry_price") or trade.get("planned_entry_price")
                quantity = trade.get("quantity")
                lot_size = trade.get("lot_size")
                try:
                    lots = f"{int(quantity) // int(lot_size)} lots x {int(lot_size)} = {int(quantity)} units"
                    amount = f"Rs {float(quantity) * float(unit_price):,.2f}"
                except (TypeError, ValueError, ZeroDivisionError):
                    lots, amount = "-", "-"
                lines += [
                    "",
                    f"Paper trade [{trade.get('trade_status') or trade.get('signal_status') or '-'}]  {trade.get('option_symbol') or ''}",
                    f"  Size: {lots}",
                    f"  Entry price: {_num(unit_price)}    Amount (premium): {amount}",
                    f"  Target: {_num(trade.get('target_1_price'))}  ({_pct(trade.get('target_1_pct'))})",
                    f"  Stop loss: {_num(trade.get('stop_loss_price'))}  ({_pct(trade.get('stop_loss_pct'))})",
                ]
        else:
            reason = summary.get("no_trade_reason")
            lines += ["", "No option trade for this date." + (f" Reason: {reason}" if reason else "")]
    lines += ["", attachment_note]
    text = "\n".join(lines)
    page = f'<pre style="font-family:Consolas,Menlo,monospace;font-size:13px;line-height:1.5">{html.escape(text)}</pre>'
    return subject, text, page


def fetch_trades(db, trade_date: date, symbol: str, model_version: str) -> list[dict]:
    return db.list_paper_trade_results(
        trade_date=trade_date, statuses=("PLANNED", "OPEN", "CLOSED", "FAILED"), symbol=symbol, model_version=model_version
    )


def fetch_news_csv(trade_date: date) -> tuple[bytes | None, str]:
    """Return (csv bytes or None, one-line note for the email body). Never raises."""
    base = (os.getenv("NEWS_SERVICE_URL") or "").strip().rstrip("/")
    key = (os.getenv("NEWS_API_KEY") or "").strip()
    if not base or not key:
        return None, "News article attachment unavailable: NEWS_SERVICE_URL / NEWS_API_KEY are not set on this service."
    try:
        response = requests.get(f"{base}/v1/signal/{trade_date.isoformat()}/csv", headers={"X-API-Key": key}, timeout=60)
    except requests.RequestException as exc:
        return None, f"News article attachment unavailable: could not reach the news service ({type(exc).__name__})."
    if response.status_code == 404:
        return None, "News article attachment unavailable: the news service has no stored signal for this date (its 08:30 run may have failed)."
    if response.status_code != 200:
        return None, f"News article attachment unavailable: the news service answered {response.status_code}."
    rows = max(0, response.content.count(b"\n") - 1)
    return response.content, f"Attached: news_articles_{trade_date.isoformat()}.csv ({rows} rows: each article with its Gemini score and seed)."


def main() -> int:
    parser = argparse.ArgumentParser(description="Send the morning prediction and news-score email.")
    parser.add_argument("--trade-date", default=None, help="Execution date YYYY-MM-DD. Default: today (IST)")
    parser.add_argument("--underlying", default="NIFTY")
    parser.add_argument("--model-version", default="cascade_v1")
    parser.add_argument("--dry-run", action="store_true", help="Print the email instead of sending it")
    args = parser.parse_args()

    if not args.trade_date and skip_if_weekend("Morning email"):
        return 0
    trade_date = date.fromisoformat(args.trade_date) if args.trade_date else _default_trade_date()
    symbol = args.underlying.upper()

    db = get_database_client(get_settings())
    db.connect()
    try:
        summary = fetch_signal_summary(db.conn, symbol, args.model_version, trade_date)
        trades = fetch_trades(db, trade_date, symbol, args.model_version)
    finally:
        db.close()

    csv_bytes, note = fetch_news_csv(trade_date)
    subject, text, page = build_message(trade_date, symbol, summary, trades, note)
    if args.dry_run:
        print(subject + "\n\n" + text)
        return 0
    attachments = [(f"news_articles_{trade_date.isoformat()}.csv", csv_bytes)] if csv_bytes else None
    try:
        message_id = mailer.send_email(subject, text, page, attachments)
    except RuntimeError as exc:
        print(f"Email not sent: {exc}", file=sys.stderr)
        return 1
    print(f"Email sent to {', '.join(mailer.recipients())}: {subject} (id {message_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
