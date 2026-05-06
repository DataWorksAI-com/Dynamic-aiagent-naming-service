"""
agentns.auth
============
Security headers middleware for agentns.

Injects standard security headers into every HTTP response.
"""

from __future__ import annotations

from typing import Callable

from fastapi import Request, Response


async def security_headers_middleware(request: Request, call_next: Callable) -> Response:
    """
    ASGI middleware — adds security headers to every response.

    Headers added:
        X-Content-Type-Options: nosniff
        X-Frame-Options:        DENY
        X-XSS-Protection:       1; mode=block
        Referrer-Policy:        no-referrer
        Cache-Control:          no-store
    """
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"]        = "DENY"
    response.headers["X-XSS-Protection"]       = "1; mode=block"
    response.headers["Referrer-Policy"]        = "no-referrer"
    response.headers["Cache-Control"]          = "no-store"
    return response
