"""Public demo mode: a read-only, rate-limited copilot that strangers can try (docs/public-demo.md).

Everyone signs in with the shared demo accounts, so the server, not the UI, refuses every change:
only signing in, switching workspace and asking are allowed. Questions are limited per visitor
and the number answered at once is capped, so one visitor can't keep the LLM busy for everyone.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from copilot.config import Settings
from copilot.users import DEMO_USERS

# The only requests that may change something in a public demo. Everything else that isn't a
# read (GET/HEAD/OPTIONS) is refused, including routes added later.
ALLOWED_WRITES = {("POST", "/auth/login"), ("POST", "/auth/switch-tenant"), ("POST", "/ask")}
READ_METHODS = {"GET", "HEAD", "OPTIONS"}
# Keep "password" out of this text: the web UI treats a 403 that mentions a password change as
# "you must change your password first".
DISABLED = (
    "This is a read-only public demo, so changes are turned off. You can sign in, switch"
    " workspace and ask questions."
)


def client_ip(settings: Settings, request: Request) -> str:
    if settings.client_ip_header:
        forwarded = request.headers.get(settings.client_ip_header, "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"


class RateLimiter:
    """Sliding-window limits per key: at most ``per_minute`` and ``per_day`` events."""

    def __init__(self, per_minute: int, per_day: int) -> None:
        self.limits = [(60.0, per_minute), (86_400.0, per_day)]
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, now: float | None = None) -> float:
        """Record an event. Returns 0 if allowed, else the seconds until the next one is."""
        now = time.time() if now is None else now
        with self._lock:
            events = self._events.setdefault(key, deque())
            while events and now - events[0] >= 86_400.0:
                events.popleft()
            for window, limit in self.limits:
                recent = [t for t in events if now - t < window]
                if len(recent) >= limit:
                    return max(1.0, window - (now - recent[0]))
            events.append(now)
            if len(self._events) > 10_000:  # forget visitors with nothing left in the day window
                for k in [k for k, v in self._events.items() if not v]:
                    del self._events[k]
            return 0.0


def accounts(settings: Settings) -> list[dict]:
    return [
        {"username": name, "role": role.label, "password": settings.demo_password}
        for name, role in DEMO_USERS
    ]


def check_settings(settings: Settings) -> None:
    """Refuse to start a public demo with settings that would let visitors in further."""
    if settings.dev_token_endpoint:
        raise RuntimeError("COPILOT_PUBLIC_DEMO needs COPILOT_DEV_TOKEN_ENDPOINT=false")
    if (
        settings.jwt_secret.startswith(("change-me", "replace-with"))
        or len(settings.jwt_secret) < 32
    ):
        raise RuntimeError(
            "COPILOT_PUBLIC_DEMO needs a random COPILOT_JWT_SECRET of 32+ characters"
        )


def install(app: FastAPI, settings: Settings) -> None:
    """Add the read-only guard and the /ask limits to ``app``."""
    limiter = RateLimiter(settings.demo_asks_per_minute, settings.demo_asks_per_day)
    slots = asyncio.Semaphore(max(1, settings.demo_max_concurrent))

    def too_many(detail: str, retry_after: float) -> JSONResponse:
        return JSONResponse(
            {"detail": detail}, status_code=429, headers={"Retry-After": str(int(retry_after))}
        )

    @app.middleware("http")
    async def public_demo_guard(request: Request, call_next):
        method, path = request.method, request.url.path
        if method in READ_METHODS:
            return await call_next(request)
        if (method, path) not in ALLOWED_WRITES:
            return JSONResponse({"detail": DISABLED}, status_code=403)
        if path != "/ask":
            return await call_next(request)
        wait = limiter.hit(client_ip(settings, request))
        if wait:
            return too_many(
                f"Demo limit reached ({settings.demo_asks_per_minute} questions a minute,"
                f" {settings.demo_asks_per_day} a day). Try again in {int(wait)} seconds.",
                wait,
            )
        try:
            await asyncio.wait_for(slots.acquire(), settings.demo_queue_wait_s)
        except TimeoutError:
            return too_many("The demo is busy answering other visitors. Try again in a minute.", 60)
        try:
            return await call_next(request)
        finally:
            slots.release()
