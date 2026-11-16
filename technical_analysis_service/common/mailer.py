"""Outbound email through Resend's HTTP API.

Environment (set on the service, never committed):
    EMAIL_PROVIDER    must be "resend" (the only provider implemented)
    RESEND_API_KEY    Resend API key
    EMAIL_FROM        a sender on a domain verified in Resend, e.g. "Stockie <signals@yourdomain.com>"
    NOTIFY_EMAIL_TO   recipient(s), separated by comma, semicolon or space
"""
from __future__ import annotations

import base64
import os

import requests

API_URL = "https://api.resend.com/emails"


def recipients() -> list[str]:
    raw = os.getenv("NOTIFY_EMAIL_TO", "")
    return [part.strip() for part in raw.replace(",", " ").replace(";", " ").split() if part.strip()]


def missing_settings() -> list[str]:
    missing = []
    if (os.getenv("EMAIL_PROVIDER") or "").strip().lower() != "resend":
        missing.append("EMAIL_PROVIDER=resend")
    for name in ("RESEND_API_KEY", "EMAIL_FROM"):
        if not (os.getenv(name) or "").strip():
            missing.append(name)
    if not recipients():
        missing.append("NOTIFY_EMAIL_TO")
    return missing


def send_email(
    subject: str,
    text: str,
    html: str | None = None,
    attachments: list[tuple[str, bytes]] | None = None,
) -> str:
    """Send one email and return Resend's message id. Raises RuntimeError when misconfigured or rejected."""
    missing = missing_settings()
    if missing:
        raise RuntimeError("Email is not configured; set: " + ", ".join(missing))
    payload: dict = {"from": os.environ["EMAIL_FROM"].strip(), "to": recipients(), "subject": subject, "text": text}
    if html:
        payload["html"] = html
    if attachments:
        payload["attachments"] = [
            {"filename": name, "content": base64.b64encode(content).decode("ascii")} for name, content in attachments
        ]
    response = requests.post(
        API_URL,
        json=payload,
        headers={"Authorization": "Bearer " + os.environ["RESEND_API_KEY"].strip(), "User-Agent": "stockie-technical-analysis"},
        timeout=30,
    )
    if response.status_code >= 300:
        raise RuntimeError(f"Resend rejected the email ({response.status_code}): {response.text[:300]}")
    return str(response.json().get("id", ""))
