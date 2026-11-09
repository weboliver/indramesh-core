"""Stateless refresh credentials for the browser session (SESS-01/SESS-02, D-01/D-03).

The long-lived half of a browser session lives in an ``HttpOnly`` /
``SameSite=Strict`` cookie signed with the same HS256 key as the access token.
The credential is otherwise stateless — no session table — so every lifetime
bound and every revocation decision has to be expressed as a claim:

* ``sid``   — the session id, blocklisted at logout (and reused by every
              rotation, so one logout ends the whole chain);
* ``token_version`` — the per-user epoch (SESS-02): a password change bumps the
              row, and the refresh route compares the claim against it;
* ``abs``   — the absolute deadline measured from the first login. Rotation
              copies it verbatim, so the sliding idle window can never extend a
              session past ``REFRESH_ABSOLUTE_SECONDS``;
* ``exp``   — the sliding idle deadline (``REFRESH_IDLE_SECONDS`` from the last
              rotation, capped by ``abs``);
* ``typ``   — ``"refresh"``. Access and refresh credentials share one HS256 key,
              so without this claim an 8-hour cookie value would be accepted as
              a bearer token (T-321-29).

The module deliberately imports ``auth_service`` lazily inside the signing
helpers: ``auth_service`` imports this module at module level (so ``login()``
can mint a session id), and a top-level import back would be a cycle.
"""

import time
import uuid
from dataclasses import dataclass

import jwt
from starlette.requests import Request
from starlette.responses import Response

SESSION_COOKIE_NAME = "indramesh_refresh"
SESSION_COOKIE_PATH = "/api/v1/auth"
REFRESH_IDLE_SECONDS = 8 * 60 * 60
REFRESH_ABSOLUTE_SECONDS = 30 * 24 * 60 * 60
REFRESH_ALGORITHM = "HS256"
REFRESH_COOKIE_SAMESITE = "strict"

_REQUIRED_CLAIMS = ("sub", "sid", "token_version", "abs", "exp", "jti", "typ")


class SessionExpiredError(Exception):
    """The credential's absolute deadline (``abs``) has passed.

    Kept distinct from ``jwt.ExpiredSignatureError`` so the route can map both to
    the same 401 while the two lifetime bounds stay independently testable.
    """


@dataclass(frozen=True)
class RefreshClaims:
    """The validated claim set of a presented refresh credential."""

    user_id: str
    sid: str
    token_version: int
    absolute_deadline: int
    jti: str


def new_session_id() -> str:
    """A fresh session id (opaque, never derived from the user)."""
    return uuid.uuid4().hex


def refresh_absolute_deadline(issued_at: int) -> int:
    """The hard upper bound of a session that started at ``issued_at``."""
    return int(issued_at) + REFRESH_ABSOLUTE_SECONDS


def remaining_session_seconds(absolute_deadline: int) -> int:
    """Seconds until ``absolute_deadline``, never negative (blocklist TTL)."""
    return max(0, int(absolute_deadline) - int(time.time()))


def build_refresh_token(
    *,
    user_id: str,
    sid: str,
    token_version: int,
    absolute_deadline: int,
) -> str:
    """Sign a refresh credential.

    ``exp`` is the sliding idle window capped by the session's absolute
    deadline, so a rotated cookie can never outlive ``abs``.
    """
    from app.services.auth_service import get_secret_key

    now = int(time.time())
    deadline = int(absolute_deadline)
    payload = {
        "sub": str(user_id),
        "sid": sid,
        "token_version": int(token_version),
        "abs": deadline,
        "iat": now,
        "exp": min(now + REFRESH_IDLE_SECONDS, deadline),
        "jti": str(uuid.uuid4()),
        "typ": "refresh",
    }
    return jwt.encode(payload, get_secret_key(), algorithm=REFRESH_ALGORITHM)


def decode_refresh_token(token: str) -> RefreshClaims:
    """Validate a presented refresh credential and return its claims.

    Raises ``jwt.InvalidTokenError`` for a bad signature or a missing/foreign
    ``typ`` (an access token placed in the cookie is refused here), 
    ``jwt.ExpiredSignatureError`` for a passed idle ``exp``, and
    ``SessionExpiredError`` when ``abs`` has passed even though ``exp`` has not.
    """
    from app.services.auth_service import get_secret_key

    payload = jwt.decode(
        token,
        get_secret_key(),
        algorithms=[REFRESH_ALGORITHM],
        options={"require": list(_REQUIRED_CLAIMS)},
    )
    if payload.get("typ") != "refresh":
        raise jwt.InvalidTokenError("credential is not a refresh token")

    absolute_deadline = int(payload["abs"])
    if int(time.time()) >= absolute_deadline:
        raise SessionExpiredError("refresh credential exceeded its absolute lifetime")

    return RefreshClaims(
        user_id=str(payload["sub"]),
        sid=str(payload["sid"]),
        token_version=int(payload["token_version"]),
        absolute_deadline=absolute_deadline,
        jti=str(payload["jti"]),
    )


def cookie_is_secure(request: Request) -> bool:
    """``Secure`` must follow the request scheme, never a constant.

    The E2E suite calls ``http://backend:8000`` directly (frontend/e2e/
    auth.spec.js), so an unconditional ``Secure`` would drop the cookie on that
    path while a plaintext deployment would ship it in the clear.
    """
    return request.url.scheme == "https"


def set_refresh_cookie(
    response: Response,
    request: Request,
    *,
    user_id: str,
    sid: str,
    token_version: int,
    absolute_deadline: int,
) -> None:
    """Attach the refresh credential to the response the route actually returns."""
    token = build_refresh_token(
        user_id=user_id,
        sid=sid,
        token_version=token_version,
        absolute_deadline=absolute_deadline,
    )
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=max(0, int(absolute_deadline) - int(time.time())),
        path=SESSION_COOKIE_PATH,
        secure=cookie_is_secure(request),
        httponly=True,
        samesite=REFRESH_COOKIE_SAMESITE,
    )


def clear_refresh_cookie(response: Response) -> None:
    """Expire the refresh credential (same name/path, ``Max-Age=0``)."""
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path=SESSION_COOKIE_PATH,
        httponly=True,
        samesite=REFRESH_COOKIE_SAMESITE,
    )
