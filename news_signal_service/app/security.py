from __future__ import annotations

import hmac

from fastapi import Depends, Header, HTTPException, status

from app.config import Settings, get_settings


def require_api_key(
    x_api_key: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """Protects every /v1 route. /health stays open for Railway's healthcheck."""
    if settings.allow_unauthenticated:
        return
    valid = settings.api_key_set()
    if not valid:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "API_KEYS is not configured on the server.")
    if not x_api_key or not any(hmac.compare_digest(x_api_key, key) for key in valid):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing X-API-Key.")
