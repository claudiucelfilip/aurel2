"""Fail-closed dashboard identities and browser approval protection."""

import base64
import hashlib
import hmac
import os
import re
import time

from fastapi import Request
from fastapi.responses import JSONResponse


def _equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def _credential(prefix: str) -> tuple[str, str]:
    return os.getenv(prefix + "_USER", ""), os.getenv(prefix + "_PASSWORD", "")


def csrf_token(decision_id: str, timestamp: int | None = None) -> str:
    stamp = int(time.time()) if timestamp is None else timestamp
    message = f"{stamp}:{decision_id}"
    secret = os.environ.get("AUREL2_DASHBOARD_PASSWORD", "")
    signature = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return f"{stamp}.{signature}"


def valid_csrf(token: str, decision_id: str) -> bool:
    try:
        stamp = int(token.split(".")[0])
        return 0 <= time.time() - stamp <= 900 and _equal(token, csrf_token(decision_id, stamp))
    except (ValueError, TypeError):
        return False


async def protect_dashboard(request: Request, call_next):
    approver, password = _credential("AUREL2_DASHBOARD")
    reader, read_password = _credential("AUREL2_DASHBOARD_READ")
    publisher = os.getenv("AUREL2_APPROVAL_API_TOKEN", "")
    authorization = request.headers.get("authorization", "")
    role = None
    if authorization.startswith("Basic "):
        try:
            user, supplied = base64.b64decode(authorization[6:], validate=True).decode().split(":", 1)
            if approver and len(password) >= 32 and _equal(user, approver) and _equal(supplied, password):
                role = "approver"
            elif reader and len(read_password) >= 32 and _equal(user, reader) and _equal(supplied, read_password):
                role = "reader"
        except (ValueError, UnicodeError):
            pass
    elif authorization.startswith("Bearer ") and len(publisher) >= 32 and _equal(authorization[7:], publisher):
        role = "publisher"

    if role is None:
        return JSONResponse({"error": "authentication required"}, 401, headers={
            "WWW-Authenticate": 'Basic realm="Aurel2", charset="UTF-8"', "Cache-Control": "no-store",
        })
    path, method = request.url.path, request.method
    if role == "publisher" and not (
        re.fullmatch(r"/decision/[A-Za-z0-9_-]+", path) and method in ("GET", "POST")
        or re.fullmatch(r"/api/decision/[A-Za-z0-9_-]+", path) and method == "GET"
    ):
        return JSONResponse({"error": "forbidden"}, 403)
    if method not in ("GET", "HEAD", "OPTIONS") and role != "publisher":
        if role != "approver":
            return JSONResponse({"error": "forbidden"}, 403)
        match = re.fullmatch(r"/api/decision/([A-Za-z0-9_-]+)/(approve|reject)", path)
        token = request.headers.get("x-csrf-token", "")
        if not token and request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
            token = str((await request.form()).get("csrf_token", ""))
        origin = request.headers.get("origin")
        expected = (os.getenv("AUREL2_DASHBOARD_ORIGIN") or str(request.base_url).rstrip("/"))
        if (not match or not valid_csrf(token, match[1])
                or origin and origin != expected
                or request.headers.get("sec-fetch-site") == "cross-site"):
            return JSONResponse({"error": "invalid request token or origin"}, 403)
    request.state.approval_role = role
    response = await call_next(request)
    response.headers.update({
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    })
    return response
