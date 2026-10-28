import hashlib
import os

from slowapi import Limiter
from slowapi.util import get_remote_address


def get_rate_limit_key(request) -> str:
    """Per-user for authenticated requests, per-IP for anonymous.

    Public-realm (imsh_) requests key on SHA-256 of the full presented token
    (D-12): per-token buckets, so two ApiClients behind one egress IP are
    limited independently (AUTH-06). Plaintext tokens never enter nginx shared
    memory — nginx keys on $binary_remote_addr only (D-09); the exact control
    happens here on a hash.

    IP resolution relies entirely on ProxyHeadersMiddleware (registered in main.py)
    having already corrected request.client.host from a *trusted* proxy's
    X-Forwarded-For header. This function must not re-parse X-Forwarded-For itself.
    """
    auth_header = request.headers.get("Authorization", "")
    parts = auth_header.split(None, 1)
    # D-10: the scheme word is matched case-insensitively — a lowercase
    # "bearer imsh_..." / "bearer <jwt>" header previously fell through both
    # startswith("Bearer ") branches into ip: keying (per-token isolation
    # bypass: N per-token buckets collapsed into one shared per-IP bucket).
    if len(parts) == 2 and parts[0].lower() == "bearer":
        token = parts[1]
        # D-12: imsh_ branch must come BEFORE the generic "Bearer " branch —
        # "Bearer imsh_..." also matches the generic bearer branch. The hash is
        # computed over the FULL presented token string (scheme stripped only),
        # so the Plan 03 auth lookup agrees on the same hash. The imsh_ prefix
        # check itself stays exact-lowercase (D-11): an IMSH_ token falls
        # through to the JWT attempt below, then to ip: keying.
        if token.startswith("imsh_"):
            return f"public:{hashlib.sha256(token.encode()).hexdigest()}"
        try:
            import jwt
            from app.services.auth_service import get_secret_key, ALGORITHM

            payload = jwt.decode(token, get_secret_key(), algorithms=[ALGORITHM])
            return f"user:{payload.get('sub', 'unknown')}"
        except Exception:
            pass
    return f"ip:{get_remote_address(request)}"


_RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
# Phase 312.1 (D-15/D-16): operator-visible name renamed; the `redis://` scheme (protocol
# name) and the limiter's storage-URI contract are unchanged.
_VALKEY_URL = os.getenv("VALKEY_URL", "redis://valkey:6379/0")

# D-10: per-token default for the public realm; consumed by the global
# middleware default (D-17) and exported for tests.
PUBLIC_API_RATE_LIMIT = os.getenv("PUBLIC_API_RATE_LIMIT", "200/minute")

# D-17: the global middleware default is pinned to the public limit —
# both surfaces must enforce the same bucket (default 200/minute).
# RATE_LIMIT_GLOBAL may still be overridden by env (operators can raise it
# explicitly); the pin is only the DEFAULT, so an unset env can never
# silently diverge from the public limit.
limiter = Limiter(
    key_func=get_rate_limit_key,
    default_limits=[os.getenv("RATE_LIMIT_GLOBAL", PUBLIC_API_RATE_LIMIT)],
    headers_enabled=True,
    storage_uri=os.getenv("RATE_LIMIT_STORAGE_URI", _VALKEY_URL),
    strategy="fixed-window",
    enabled=_RATE_LIMIT_ENABLED,
    in_memory_fallback=[os.getenv("RATE_LIMIT_FALLBACK", "50/minute")],
    in_memory_fallback_enabled=True,
)

# Phase 96 — Login-only limiter: ALWAYS active regardless of RATE_LIMIT_ENABLED.
# Closes the security gap where rate limiting was bypassed on backend-direct port :8000
# (RATELIMIT-01). Uses get_remote_address (pre-auth) rather than get_rate_limit_key
# (which tries JWT decode first — irrelevant for login where there is no bearer token).
# No default_limits — only used via @login_limiter.limit() on specific routes.
login_limiter = Limiter(
    key_func=get_remote_address,
    headers_enabled=True,
    storage_uri=os.getenv("RATE_LIMIT_STORAGE_URI", _VALKEY_URL),
    enabled=True,  # Always on — RATE_LIMIT_ENABLED has no effect here
)
