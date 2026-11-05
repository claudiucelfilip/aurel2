"""Shared approval transport policy; credentials never travel in links."""

import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit


def approval_headers(url: str) -> dict[str, str]:
    parsed = urlsplit(url)
    token = os.getenv("AUREL2_APPROVAL_API_TOKEN", "")
    if len(token) < 32:
        raise ValueError("AUREL2_APPROVAL_API_TOKEN must contain at least 32 characters")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Approval URL must not contain credentials, query or fragment")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("Approval endpoint requires HTTPS except on loopback")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def approval_deadline(created_at: str) -> datetime:
    created = datetime.fromisoformat(created_at)
    if created.tzinfo is None:
        created = created.astimezone()
    return created + timedelta(hours=1)


def approval_expired(created_at: str) -> bool:
    try:
        return datetime.now(timezone.utc) >= approval_deadline(created_at)
    except (ValueError, TypeError):
        return True
