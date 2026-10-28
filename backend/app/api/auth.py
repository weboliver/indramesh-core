import os
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
import jwt
from sqlalchemy import select

from app.db import async_session_maker, get_db
from app.models import Permission
from app.schemas.auth import Token, UserResponse
from app.services.auth_service import (
    get_current_user, login, resolve_user_permissions,
    get_secret_key, add_token_to_blocklist, bump_token_version, ALGORITHM
)
from app.services.rate_limit import limiter, login_limiter
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=Token)
@login_limiter.limit(os.getenv("RATE_LIMIT_LOGIN", "5/minute"))
async def login_handler(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
):
    token = await login(form_data)
    return JSONResponse(content=token.model_dump(), status_code=200)


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
