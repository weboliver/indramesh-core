import os
import time
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
import jwt
from sqlalchemy import select

from app.db import async_session_maker, get_db
from app.models import Permission, User
from app.schemas.auth import Token, UserResponse
from app.services import session_service
from app.services.auth_service import (
    get_current_user, login_with_session, resolve_user_permissions,
    create_access_token, get_secret_key, add_token_to_blocklist,
    add_session_to_blocklist, blocklist_state, BlocklistState,
    SESSION_BLOCKLIST_PREFIX, bump_token_version, ALGORITHM
)
from app.services.rate_limit import limiter, login_limiter
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/auth", tags=["auth"])


def _invalid_credentials() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


@router.post("/login", response_model=Token)
@login_limiter.limit(os.getenv("RATE_LIMIT_LOGIN", "5/minute"))
async def login_handler(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
):
    sid = session_service.new_session_id()
    outcome = await login_with_session(form_data, sid=sid)
    response = JSONResponse(content=outcome.token.model_dump(), status_code=200)
    session_service.set_refresh_cookie(
        response,
        request,
        user_id=str(outcome.user.id),
        sid=sid,
        token_version=outcome.user.token_version,
        absolute_deadline=session_service.refresh_absolute_deadline(int(time.time())),
    )
    return response


@router.post("/refresh", response_model=Token)
async def refresh_handler(request: Request):
    """Rotate the refresh credential and mint a fresh access token.

    The session id and the absolute deadline are copied from the presented
    credential — rotation must never fork a second session or extend the hard
    upper bound. Blocklist, epoch and user-state checks live here because this
    route is the only path that accepts the cookie.
    """
    presented = request.cookies.get(session_service.SESSION_COOKIE_NAME)
    if not presented:
        raise _invalid_credentials()

    try:
        claims = session_service.decode_refresh_token(presented)
    except jwt.ExpiredSignatureError:
        raise _invalid_credentials()
    except session_service.SessionExpiredError:
        raise _invalid_credentials()
    except jwt.PyJWTError:
        raise _invalid_credentials()

    state = await blocklist_state(f"{SESSION_BLOCKLIST_PREFIX}{claims.sid}")
    if state is BlocklistState.UNAVAILABLE:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication temporarily unavailable",
            headers={"Retry-After": "5"},
        )
    if state is BlocklistState.REVOKED:
        raise _invalid_credentials()

    # SESS-02 — the epoch is re-read from the row on every rotation, so a
    # password change ends the refresh path too (and no blocklist key is needed).
    try:
        user_uuid = uuid.UUID(claims.user_id)
    except (TypeError, ValueError):
        raise _invalid_credentials()

    async with async_session_maker() as session:
        result = await session.execute(select(User).where(User.id == user_uuid))
        user = result.scalar_one_or_none()

    if user is None or not user.is_active:
        raise _invalid_credentials()
    if claims.token_version != user.token_version:
        raise _invalid_credentials()

    # Mint from the ROW's version, never from the claim: the claim only proves
    # which epoch the credential was issued in.
    access_token = create_access_token(
        data={
            "sub": str(user.id),
            "username": user.username,
            "token_version": user.token_version,
            "sid": claims.sid,
        }
    )
    response = JSONResponse(
        content=Token(access_token=access_token, token_type="bearer").model_dump(),
        status_code=200,
    )
    session_service.set_refresh_cookie(
        response,
        request,
        user_id=claims.user_id,
        sid=claims.sid,
        token_version=claims.token_version,
        absolute_deadline=claims.absolute_deadline,
    )
    return response


@router.post("/logout", status_code=200)
async def logout_handler(request: Request):
    """End the browser session locally and server-side.

    The cookie is expired on the response BEFORE any store write, so a client is
    never left holding a credential it cannot shed when the revocation store is
    unreachable. An unreachable store answers 503 with the cookie still cleared,
    rather than a 500 that would leave the session alive in the browser.
    """
    response = JSONResponse(content={"message": "Signed out"}, status_code=200)
    session_service.clear_refresh_cookie(response)

    presented = request.cookies.get(session_service.SESSION_COOKIE_NAME)
    claims = None
    if presented:
        try:
            claims = session_service.decode_refresh_token(presented)
        except (jwt.PyJWTError, session_service.SessionExpiredError):
            claims = None

    if claims is None:
        return response

    try:
        await add_session_to_blocklist(
            claims.sid,
            session_service.remaining_session_seconds(claims.absolute_deadline),
        )
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.removeprefix("Bearer ")
            try:
                payload = jwt.decode(token, get_secret_key(), algorithms=[ALGORITHM])
                jti = payload.get("jti")
                exp = payload.get("exp")
                if jti and exp:
                    remaining = int(exp - datetime.now(timezone.utc).timestamp())
                    if remaining > 0:
                        await add_token_to_blocklist(jti, remaining)
            except jwt.PyJWTError:
                pass
    except (RedisError, RuntimeError):
        failure = JSONResponse(
            content={"message": "Signed out locally; revocation store unavailable"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
        session_service.clear_refresh_cookie(failure)
        return failure

    return response


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: UserResponse = Depends(get_current_user)):
    return current_user


@router.get("/me/permissions", response_model=list[str])
async def get_my_permissions(current_user: UserResponse = Depends(get_current_user)):
    """Return the caller's own effective permission set (self-introspection).

    Superusers get the full sorted list of every permission name in the system
    (D-04 — they implicitly have all permissions, not scoped to any role and
    not a wildcard marker). Non-superusers get their resolved deny-aware
    permission set via resolve_user_permissions() (D-05/D-07). Requires only
    authentication, not require_permission — every authenticated user may
    call this to see what they can do.
    """
    if current_user.is_superuser:
        async with async_session_maker() as session:
            result = await session.execute(select(Permission.name))
            return sorted(name for (name,) in result.all())
    return sorted(await resolve_user_permissions(current_user.id))


@router.post("/revoke", status_code=200)
async def revoke_token(
    request: Request,
    current_user: UserResponse = Depends(get_current_user)
):
    """Revoke the current request's bearer token.

    Extracts the JWT from the Authorization header, decodes it to find its
    `jti` claim, and adds it to the Redis blocklist with a TTL matching
    the token's remaining lifetime.

    Any authenticated user may revoke their own token this way (self-service
    logout / compromised-token response) — this is not an admin-only action.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing or malformed Authorization header"
        )

    token = auth_header.removeprefix("Bearer ")

    try:
        payload = jwt.decode(token, get_secret_key(), algorithms=[ALGORITHM])
        jti = payload.get("jti")
        exp = payload.get("exp")
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid token"
        )

    if not jti:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token has no jti claim"
        )

    # Calculate remaining TTL in seconds
    now_ts = datetime.now(timezone.utc).timestamp()
    remaining = int(exp - now_ts) if exp and exp > now_ts else 0

    if remaining > 0:
        await add_token_to_blocklist(jti, remaining)

    return {"message": "Token revoked", "jti": jti}


@router.post("/revoke-all", status_code=200)
async def revoke_all_tokens(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """Log the authenticated user out everywhere (D-09).

    Increments the user's token version, which invalidates every live token of
    that user — including the one that made this call, since
    ``get_current_user`` refuses a token whose claim no longer matches the row.
    Per-token ``/auth/revoke`` keeps working; this route is additive.
    """
    new_version = await bump_token_version(db, current_user.id)
    return {"message": "All sessions revoked", "token_version": new_version}
