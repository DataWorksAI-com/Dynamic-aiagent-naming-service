"""
agentns.auth
============
API key authentication and security headers middleware for agentns services.

Usage
-----
    from agentns.auth import verify_api_key, security_headers_middleware, validate_secrets_on_startup

    # Attach security headers to every response:
    app.middleware("http")(security_headers_middleware)

    # Protect an endpoint with API key auth:
    from fastapi import Depends
    @app.post("/resolve", dependencies=[Depends(verify_api_key)])
    async def resolve(...): ...

    # Validate secrets at startup (call inside lifespan or at module level):
    validate_secrets_on_startup()

Configuration
-------------
    AGENTNS_API_KEYS  — comma-separated list of valid API keys (≥32 chars each).
                        Supports key rotation — all keys in the list are accepted.
                        Example: AGENTNS_API_KEYS="key1-abc123...,key2-def456..."

    AGENTNS_AUTH      — "on" (default) enforces auth.
                        "off" disables auth entirely (development only — never use in prod).

Generating a secure key
-----------------------
    python -c "import secrets; print(secrets.token_urlsafe(32))"
"""

from __future__ import annotations

import logging
import os
from typing import Callable

from fastapi import Header, HTTPException, Request, Response

logger = logging.getLogger("agentns.auth")


# ── Internal helpers ───────────────────────────────────────────────────────────

def _get_valid_keys() -> set:
    """Read and return the set of valid API keys from AGENTNS_API_KEYS env var."""
    raw = os.getenv("AGENTNS_API_KEYS", "")
    return {k.strip() for k in raw.split(",") if k.strip()}


def _auth_enabled() -> bool:
    """Return True unless AGENTNS_AUTH is explicitly disabled."""
    val = os.getenv("AGENTNS_AUTH", "on").lower()
    return val not in ("off", "false", "0", "disabled", "open")


# ── FastAPI dependency ─────────────────────────────────────────────────────────

async def verify_api_key(
    request: Request,
    x_api_key: str = Header(default="", alias="X-API-Key"),
) -> str:
    """
    FastAPI dependency — validates the X-API-Key request header.

    Returns the validated key on success.
    Raises HTTP 401 on failure.
    Raises HTTP 503 if AGENTNS_API_KEYS is not configured.

    Auth is skipped when AGENTNS_AUTH=off (development mode).

    Usage:
        @app.post("/resolve", dependencies=[Depends(verify_api_key)])
        async def resolve(body: dict): ...
    """
    if not _auth_enabled():
        return x_api_key  # auth disabled — pass through

    valid_keys = _get_valid_keys()

    if not valid_keys:
        logger.error(
            "AGENTNS_API_KEYS is empty but AGENTNS_AUTH=on. "
            "Set AGENTNS_API_KEYS or use AGENTNS_AUTH=off for development."
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "Server authentication not configured. "
                "Set the AGENTNS_API_KEYS environment variable."
            ),
        )

    if x_api_key not in valid_keys:
        # Log only the first 8 chars — never log the full key
        client_ip = request.client.host if request.client else "unknown"
        prefix = (x_api_key[:8] + "...") if len(x_api_key) > 8 else "(empty)"
        logger.warning(f"auth_failure ip={client_ip} key_prefix={prefix}")
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    return x_api_key


# ── Security headers middleware ────────────────────────────────────────────────

async def security_headers_middleware(request: Request, call_next: Callable) -> Response:
    """
    ASGI middleware that injects security headers into every response.

    Attach to a FastAPI app with:
        app.middleware("http")(security_headers_middleware)

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


# ── Startup validation ─────────────────────────────────────────────────────────

def validate_secrets_on_startup(min_key_length: int = 32) -> None:
    """
    Validate that secrets meet minimum security requirements at startup.

    Call this inside the FastAPI lifespan context or at module level.
    Raises RuntimeError (crashes the server) if secrets are misconfigured.

    Checks:
    - If AGENTNS_AUTH=on: AGENTNS_API_KEYS must be set and each key ≥ min_key_length chars
    - If SLIM_SHARED_SECRET is set: must be ≥ min_key_length chars

    Parameters
    ----------
    min_key_length: Minimum acceptable key length in characters (default: 32).

    Raises
    ------
    RuntimeError — if any secret fails validation.
    """
    if not _auth_enabled():
        logger.warning(
            "AGENTNS_AUTH=off — API key authentication is DISABLED. "
            "This is only safe in local development. Never use in production."
        )
        return

    valid_keys = _get_valid_keys()
    if not valid_keys:
        raise RuntimeError(
            "AGENTNS_API_KEYS is not set but AGENTNS_AUTH=on.\n"
            "Set it to a comma-separated list of API keys (each ≥32 chars).\n"
            "  Generate: python -c \"import secrets; print(secrets.token_urlsafe(32))\"\n"
            "  For dev without auth: AGENTNS_AUTH=off"
        )

    short_keys = [k for k in valid_keys if len(k) < min_key_length]
    if short_keys:
        raise RuntimeError(
            f"All AGENTNS_API_KEYS must be ≥{min_key_length} characters. "
            f"Found {len(short_keys)} key(s) that are too short.\n"
            f"  Generate: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )

    # Validate SLIM shared secret if configured
    slim_secret = os.getenv("SLIM_SHARED_SECRET", "")
    if slim_secret and len(slim_secret) < min_key_length:
        raise RuntimeError(
            f"SLIM_SHARED_SECRET must be ≥{min_key_length} characters. "
            f"Current length: {len(slim_secret)}.\n"
            f"  Generate: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )

    logger.info(
        f"Startup secret validation passed — {len(valid_keys)} API key(s) configured."
    )
