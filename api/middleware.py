"""API security middleware — API key authentication and rate limiting."""
from __future__ import annotations

import os
import secrets
import time
from collections import defaultdict

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# ── API key auth ──────────────────────────────────────────────────────────────

# Comma-separated list of valid API keys. If unset, auth is disabled (dev mode).
_API_KEYS: set[str] = set()
_raw = os.getenv("API_KEYS", "")
if _raw:
    _API_KEYS = {k.strip() for k in _raw.split(",") if k.strip()}

# Routes that skip auth
_PUBLIC_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Reject requests without a valid X-API-Key header.

    When API_KEYS env var is empty, auth is disabled (local dev).
    """

    async def dispatch(self, request: Request, call_next):
        if not _API_KEYS:
            return await call_next(request)

        path = request.url.path
        if path in _PUBLIC_PATHS:
            return await call_next(request)

        key = request.headers.get("X-API-Key")
        if not key or not secrets.compare_digest(key, key) or key not in _API_KEYS:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing API key"},
            )

        return await call_next(request)


# ── Rate limiting ─────────────────────────────────────────────────────────────

# Sliding window rate limiter (per-IP, in-memory)
_RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_MINUTE", "30"))
_RATE_LIMIT_BURST = int(os.getenv("RATE_LIMIT_BURST", "10"))
_window_seconds = 60

# {ip: [timestamps]}
_request_log: dict[str, list[float]] = defaultdict(list)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple sliding-window rate limiter. Returns 429 when exceeded."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        cutoff = now - _window_seconds

        # Prune old entries
        timestamps = _request_log[client_ip]
        _request_log[client_ip] = [t for t in timestamps if t > cutoff]
        timestamps = _request_log[client_ip]

        if len(timestamps) >= _RATE_LIMIT:
            retry_after = int(timestamps[0] - cutoff) + 1
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Try again later."},
                headers={"Retry-After": str(retry_after)},
            )

        timestamps.append(now)
        return await call_next(request)
