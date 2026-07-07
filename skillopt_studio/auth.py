"""Optional username/password authentication for internet-facing deployments.

Enabled by setting ``STUDIO_AUTH_PASSWORD`` (username via ``STUDIO_AUTH_USERNAME``,
default ``admin``) — without it the studio behaves exactly as before (local
dev, tests). When enabled, every ``/api`` route and the FastAPI docs require a
signed session cookie obtained from ``POST /api/auth/login``. The SPA shell
and its static assets stay public (they contain no data); all data flows
through the protected API.

Sessions are stateless: the cookie is ``<expiry-ts>.<hmac-sha256>`` signed
with a key derived from the credentials, so sessions survive restarts and
there is nothing to store, while rotating the password invalidates every
outstanding session. Constant-time comparisons throughout.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter(prefix="/auth", tags=["auth"])

COOKIE_NAME = "skillopt_studio_session"
SESSION_TTL_SECONDS = 12 * 3600

# Paths that must stay reachable without a session.
_OPEN_API_PATHS = {"/api/auth/login", "/api/auth/status", "/api/health"}
_PROTECTED_EXTRA = {"/docs", "/redoc", "/openapi.json"}


def _password() -> str | None:
    return os.environ.get("STUDIO_AUTH_PASSWORD") or None


def _username() -> str:
    return os.environ.get("STUDIO_AUTH_USERNAME") or "admin"


def enabled() -> bool:
    return _password() is not None


def _signing_key() -> bytes:
    return hashlib.sha256(
        f"skillopt-studio-session:{_username()}:{_password()}".encode()
    ).digest()


def _sign(expiry: int) -> str:
    sig = hmac.new(_signing_key(), str(expiry).encode(), hashlib.sha256)
    return f"{expiry}.{sig.hexdigest()}"


def _verify(cookie: str | None) -> bool:
    if not cookie or "." not in cookie:
        return False
    expiry_s = cookie.partition(".")[0]
    if not expiry_s.isdigit():
        return False
    expected = _sign(int(expiry_s))
    if not hmac.compare_digest(cookie, expected):
        return False
    return int(expiry_s) > time.time()


def is_authenticated(request: Request) -> bool:
    return _verify(request.cookies.get(COOKIE_NAME))


async def middleware(request: Request, call_next: Any) -> Any:
    """Reject unauthenticated API/docs requests when auth is enabled."""
    if enabled():
        path = request.url.path
        guarded = (
            path.startswith("/api") and path not in _OPEN_API_PATHS
        ) or path in _PROTECTED_EXTRA
        if guarded and not is_authenticated(request):
            return JSONResponse(
                status_code=401, content={"detail": "authentication required"}
            )
    return await call_next(request)


class LoginRequest(BaseModel):
    username: str
    password: str


@router.get("/status")
def status(request: Request) -> dict[str, Any]:
    return {
        "auth_required": enabled(),
        "authenticated": (not enabled()) or is_authenticated(request),
    }


@router.post("/login")
def login(req: LoginRequest, response: Response) -> Any:
    if not enabled():
        return {"ok": True, "auth_required": False}
    user_ok = hmac.compare_digest(req.username.encode(), _username().encode())
    pass_ok = hmac.compare_digest(req.password.encode(), (_password() or "").encode())
    if not (user_ok and pass_ok):
        return JSONResponse(status_code=401, content={"detail": "用户名或密码错误"})
    expiry = int(time.time()) + SESSION_TTL_SECONDS
    response.set_cookie(
        COOKIE_NAME,
        _sign(expiry),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return {"ok": True, "expires_at": expiry}


@router.post("/logout")
def logout(response: Response) -> dict[str, Any]:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}
