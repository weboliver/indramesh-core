from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Awaitable, Callable, NamedTuple
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBearer,
    OAuth2PasswordBearer,
    OAuth2PasswordRequestForm,
)
import jwt
import bcrypt
import hashlib
import json
import os
import time
import uuid

from app.db import async_session_maker
from app.models import (
    User, UserRole, Role, RolePermission, Permission, ApiClient,
    UserGroup, GroupRole,
)
from app.schemas.auth import Token, TokenData, UserResponse
from sqlalchemy import select, or_, update

from app.db.redis import get_redis
from redis.exceptions import RedisError
from app.logging.security import (
    mark_api_client_rejected,
    mark_authenticated,
    mark_login_failed,
    mark_login_succeeded,
    mark_permission_denied,
    mark_token_rejected,
)
from app.services import cache_service
from app.services.record_permission_service import resolve_record_permission

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30 * 24 * 60  # 30 days — browser session lifetime
TOKEN_BLOCKLIST_PREFIX = "token_blocklist:"

_secret_key: str | None = None


def get_secret_key() -> str:
    """Lazily load JWT_SECRET_KEY from environment on first access.

    Caches the value after first read to avoid repeated os.environ access.
    Raises RuntimeError if not set — validated at startup in lifespan.
    """
    global _secret_key
    if _secret_key is None:
        _secret_key = os.environ.get("JWT_SECRET_KEY")
        if not _secret_key:
            raise RuntimeError(
                "JWT_SECRET_KEY environment variable is required but not set. "
                "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
            )
    return _secret_key


def get_clock_skew_tolerance() -> int:
    """Get clock-skew tolerance in seconds. Default: 10. Prevents false rejections in distributed systems."""
    return int(os.environ.get("CLOCK_SKEW_TOLERANCE_SECONDS", "10"))


def get_max_token_age() -> int | None:
    """Get max token age in seconds. None means age check disabled (default, backward compatible)."""
    val = os.environ.get("MAX_TOKEN_AGE_SECONDS")
    return int(val) if val else None

class _LoggingOAuth2PasswordBearer(OAuth2PasswordBearer):
    """Record ``reason=missing`` before FastAPI's own 401 for an absent token.

    FastAPI raises ``make_not_authenticated_error()`` before ``get_current_user``
    ever runs, so the security record for a missing/non-Bearer header is marked
    here (D-08). No status code, detail or header changes.
    """

    def make_not_authenticated_error(self) -> HTTPException:
        mark_token_rejected("missing")
        return super().make_not_authenticated_error()


oauth2_scheme = _LoggingOAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

# D-13 — public realm bearer scheme. auto_error=False so the dependency
# raises the canonical 401 itself (matches get_current_user's error shape).
public_bearer_scheme = HTTPBearer(auto_error=False)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())
    except ValueError:
        return False


def get_password_hash(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    to_encode.update({"jti": str(uuid.uuid4())})
    # D-09 — every minted token carries the user's token version (0 when the
    # caller does not know it). get_current_user compares it with the row.
    to_encode.setdefault("token_version", 0)
    # Add iat and nbf claims (Phase 99 — JWT-02)
    now = int(time.time())
    to_encode.update({"iat": now, "nbf": now})
    if expires_delta:
        expire = datetime.now(timezone.utc).replace(tzinfo=None) + expires_delta
    else:
        expire = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, get_secret_key(), algorithm=ALGORITHM)
    return encoded_jwt


async def _is_token_blocklisted(jti: str) -> bool:
    """Check if a token has been revoked via the Redis blocklist.

    Returns True if blocklisted, False if not blocklisted or if Redis is unavailable.
    Fail-open: if Redis is down, allows the request (blocklist check is defense-in-depth,
    not a primary auth control).
    """
    try:
        r = await get_redis()
        result = await r.get(f"{TOKEN_BLOCKLIST_PREFIX}{jti}")
        return result is not None
    except (RuntimeError, RedisError):
        return False


async def get_current_user(token: Annotated[str, Depends(oauth2_scheme)]) -> UserResponse:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(
            token,
            get_secret_key(),
            algorithms=[ALGORITHM],
            # PyJWT takes the clock-skew tolerance as its own keyword; an
            # options={"leeway": N} entry is silently ignored and would
            # collapse the tolerance to zero.
            leeway=get_clock_skew_tolerance(),
        )
        user_id: str = payload.get("sub")
        username: str = payload.get("username")
        if user_id is None:
            mark_token_rejected("invalid")
            raise credentials_exception
        token_data = TokenData(user_id=user_id, username=username)

        # Optional: Age-based token revocation (JWT best practice)
        max_age = get_max_token_age()
        if max_age is not None:
            iat = payload.get("iat")
            if iat is not None:
                now = int(time.time())
                token_age_seconds = now - iat
                if token_age_seconds > max_age:
                    # Token is too old, reject it
                    mark_token_rejected("expired")
                    raise credentials_exception

        # Blocklist check: reject revoked tokens (D-08: reason=invalid)
        jti = payload.get("jti")
        if jti:
            is_revoked = await _is_token_blocklisted(jti)
            if is_revoked:
                mark_token_rejected("invalid")
                raise credentials_exception

    except jwt.ExpiredSignatureError:
        mark_token_rejected("expired")
        raise credentials_exception
    except jwt.PyJWTError:
        mark_token_rejected("invalid")
        raise credentials_exception
    
    async with async_session_maker() as session:
        result = await session.execute(
            select(User).where(User.id == token_data.user_id)
        )
        user = result.scalar_one_or_none()
    
    if user is None:
        mark_token_rejected("invalid")
        raise credentials_exception

    # D-09 — "log out everywhere" / password change. The row is already loaded
    # for the is_active check above, so this comparison costs zero queries. A
    # token minted before the column existed carries no claim and is treated as
    # version 0, which is the column default.
    token_version = payload.get("token_version", 0)
    if token_version != user.token_version:
        mark_token_rejected("invalid")
        raise credentials_exception

    if not user.is_active:
        # orchestrator resolution 3: deactivated account -> reason=invalid
        mark_token_rejected("invalid")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account is deactivated"
        )
    
    mark_authenticated(str(user.id))
    return UserResponse(
        id=str(user.id),
        username=user.username,
        email=user.email,
        is_active=user.is_active,
        is_superuser=user.is_superuser
    )


class AuthAttempt(NamedTuple):
    """Outcome of a credential check without leaking the submitted username."""

    user: User | None
    failure: str | None
    known_user_id: str | None


async def _authenticate(username: str, password: str) -> AuthAttempt:
    """Resolve a login attempt, distinguishing unknown user from wrong password (D-05).

    ``failure`` is ``"unknown_user"`` for a name that does not exist and
    ``"invalid_password"`` for an existing user with a wrong password;
    ``known_user_id`` is the existing user's UUID string (never the username).
    """
    async with async_session_maker() as session:
        result = await session.execute(
            select(User).where(User.username == username)
        )
        user = result.scalar_one_or_none()

    if not user:
        return AuthAttempt(None, "unknown_user", None)
    if not verify_password(password, user.password_hash):
        return AuthAttempt(None, "invalid_password", str(user.id))
    return AuthAttempt(user, None, None)


async def authenticate_user(username: str, password: str) -> User | None:
    attempt = await _authenticate(username, password)
    return attempt.user


async def login(form_data: Annotated[OAuth2PasswordRequestForm, Depends()]) -> Token:
    attempt = await _authenticate(form_data.username, form_data.password)
    if attempt.user is None:
        # The submitted username is never logged (D-05): an existing user is
        # attributed by UUID, an unknown one only reason=unknown_user.
        mark_login_failed(
            attempt.failure or "invalid_password",
            user_id=attempt.known_user_id,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = attempt.user

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={
            "sub": str(user.id),
            "username": user.username,
            "token_version": user.token_version,
        },
        expires_delta=access_token_expires
    )
    mark_login_succeeded(str(user.id))
    return Token(access_token=access_token, token_type="bearer")


async def bump_token_version(db, user_id) -> int:
    """Increment a user's token version and return the new value (D-09).

    Ends every live token of that user, including the one that made the call:
    ``get_current_user`` refuses a token whose ``token_version`` claim no
    longer matches the row. Used by the password-change route and by
    ``POST /api/v1/auth/revoke-all`` ("log out everywhere").
    """
    result = await db.execute(
        update(User)
        .where(User.id == uuid.UUID(str(user_id)))
        .values(token_version=User.token_version + 1)
        .returning(User.token_version)
    )
    new_version = result.scalar_one()
    await db.commit()
    return new_version


async def add_token_to_blocklist(jti: str, expires_in_seconds: int) -> None:
    """Add a token's jti to the Redis blocklist with the given TTL.

    Called when revoking a token — the TTL should match the token's remaining lifetime
    so the blocklist entry is automatically cleaned up.
    """
    r = await get_redis()
    await r.set(
        f"{TOKEN_BLOCKLIST_PREFIX}{jti}",
        "revoked",
        ex=expires_in_seconds
    )


async def require_superuser(
    current_user: UserResponse = Depends(get_current_user)
) -> UserResponse:
    """FastAPI dependency that requires the authenticated user to be a superuser.

    Chains on get_current_user (which validates JWT, checks is_active, and checks
    blocklist), then additionally requires is_superuser=True.
    Returns the UserResponse on success.
    Raises HTTPException 403 if the user is not a superuser.
    """
    if not current_user.is_superuser:
        mark_permission_denied(None, reason="superuser_required")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized. Superuser access required."
        )
    return current_user


async def resolve_user_permissions(user_id: str) -> set[str]:
    """Resolve the effective permission set for a user across all of their roles.

    Deny-wins-globally (D-07): a permission granted by one role but denied by
    another (is_deny=True) is excluded, regardless of role ordering. Reads
    through the Redis permission cache (60s TTL, D-02) via cache_service, and
    falls back to a single joined DB query on cache miss/Redis error
    (cache_service.get_cached/set_cached are already fail-open).
    """
    cache_key = cache_service.build_cache_key("user_permissions", user_id)
    cached = await cache_service.get_cached(cache_key)
    if cached is not None:
        return set(cached)

    async with async_session_maker() as session:
        # D-18 (246-03): resolve permissions across BOTH the user's direct
        # user_roles AND roles inherited via group membership
        # (users_groups -> groups_roles). Deny-wins (D-07) is computed over
        # the union below — a permission granted via one role but denied via
        # another (direct or group) is excluded. Duplicate rows from the
        # outer-join union are harmless — consumed into the granted/denied
        # sets.
        result = await session.execute(
            select(Permission.name, RolePermission.is_deny)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(Role, Role.id == RolePermission.role_id)
            .outerjoin(UserRole, UserRole.role_id == Role.id)
            .outerjoin(GroupRole, GroupRole.role_id == Role.id)
            .outerjoin(UserGroup, UserGroup.group_id == GroupRole.group_id)
            .where(or_(UserRole.user_id == user_id, UserGroup.user_id == user_id))
        )
        rows = result.all()

    granted = {name for name, is_deny in rows if not is_deny}
    denied = {name for name, is_deny in rows if is_deny}
    resolved = granted - denied

    await cache_service.set_cached(cache_key, sorted(resolved), entity_type="user_permissions")
    return resolved


# Phase 303 (SEAM-02): the attribute name of the Core-owned permission marker
# attached to every dependency ``require_permission()`` returns. The mount-time
# route gate walks the ACTUAL effective dependency tree for these markers —
# never a function name and never a source-text occurrence of
# ``require_permission`` (which a plugin could satisfy with a no-op).
PERMISSION_MARKER_ATTR = "__indramesh_permission_marker__"


@dataclass(frozen=True)
class PermissionMarker:
    """The immutable ``resource``/``action`` pair a dependency enforces.

    Attached to the dependency function itself (not a wrapping closure), so the
    factory's identity export to plugins (``indramesh.sdk`` re-exports the
    returned callable unchanged) is preserved.
    """

    resource: str
    action: str

    @property
    def name(self) -> str:
        return f"{self.resource}:{self.action}"


def require_permission(resource: str, action: str) -> Callable[..., Awaitable[UserResponse]]:
    """FastAPI dependency factory requiring the authenticated user to hold
    the `{resource}:{action}` permission.

    Chains on get_current_user. Superusers bypass with zero DB/cache queries
    (PERM-05) — the is_superuser check is the first statement in the inner
    dependency. Non-superusers are checked against resolve_user_permissions().
    Raises HTTPException 403 if the permission is missing.
    """
    async def _check(
        current_user: UserResponse = Depends(get_current_user)
    ) -> UserResponse:
        if current_user.is_superuser: return current_user  # PERM-05: zero DB/cache queries for superusers

        permission_name = f"{resource}:{action}"
        user_permissions = await resolve_user_permissions(current_user.id)
        if permission_name not in user_permissions:
            mark_permission_denied(permission_name)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Not authorized. Missing permission: {permission_name}."
            )
        return current_user
    # SEAM-02: attach the Core-owned, immutable marker. Setting an attribute on
    # the returned callable leaves its identity unchanged, so plugins importing
    # the factory through the SDK see exactly the same object.
    setattr(_check, PERMISSION_MARKER_ATTR, PermissionMarker(resource=resource, action=action))
    return _check


def require_record_permission(
    resource: str,
    action: str,
    entity_type: str,
    id_param: str,
    require_type_level: bool = True,
) -> Callable[..., Awaitable[UserResponse]]:
    """FastAPI dependency factory requiring record-level permission.

    Composes type-level RBAC (require_permission) with record-level resolution
    (resolve_record_permission). Resolution order (D-05, D-06):

    1. RLR-05: Superuser bypass — zero DB/cache queries
    2. Type-level check (D-06): if type-level denies, 403 immediately
    3. Record-level check: resolve_record_permission()
    4. Interpret result:
       - False (explicit DENY) -> 403 Forbidden
       - True (explicit ALLOW) -> return current_user
       - None (no record entry) -> return current_user (permissive default)

    The id_param is read from request.path_params (e.g. 'source_id', 'object_id').

    require_type_level=False skips step 2 (used for `sources` GET, which is
    intentionally open to every authenticated user — see
    TestRbacSourcesReadWriteSplit in test_rbac.py, "sources" is an
    admin-only resource with zero Editor/Viewer type-level grants, D-02 in
    init_service.py — while still enforcing record-level DENY overrides).
    """
    async def _check(
        current_user: UserResponse = Depends(get_current_user),
        request: Request = None,
    ) -> UserResponse:
        # RLR-05: Superuser bypass — zero DB/cache queries
        if current_user.is_superuser:
            return current_user

        # D-06: Type-level check first — type-level DENY is final
        permission_name = f"{resource}:{action}"
        if require_type_level:
            user_permissions = await resolve_user_permissions(current_user.id)
            if permission_name not in user_permissions:
                mark_permission_denied(permission_name)
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Not authorized. Missing permission: {permission_name}."
                )

        # Resolve entity_id from route path parameters
        try:
            entity_id = uuid.UUID(request.path_params[id_param])
        except (KeyError, ValueError, TypeError, AttributeError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid or missing path parameter: {id_param}."
            )

        # Record-level check
        record_result = await resolve_record_permission(
            user_id=current_user.id,
            entity_type=entity_type,
            record_id=entity_id,
            action=action,
        )

        # Interpret result (RLR-04)
        if record_result is False:
            # Explicit DENY — 403 Forbidden
            mark_permission_denied(permission_name, reason="record_permission_denied")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Not authorized. Record-level permission denied for {entity_type}:{entity_id}."
            )

        # True or None: permissive default — type-level result stands
        return current_user
    return _check


def _is_expired(exp) -> bool:
    # CR-01/D-15 — corrupt-but-parseable expires_at must fail open, never 500.
    # A malformed string (e.g. "") raises ValueError; a non-str value (e.g. an
    # int) raises TypeError from fromisoformat — both degrade to "not expired"
    # here, matching the identity guard's fail-open idiom below.
    if exp is None:
        return False
    try:
        return datetime.fromisoformat(exp) < datetime.now(timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError):
        return False


# D-13 / AUTH-02 — public realm auth: resolves a static service-account
# bearer token (imsh_...) to the ApiClient's synthetic service User via
# exact-match on the SHA-256 token_hash. After this, the entire existing
# permission stack (require_permission, resolve_user_permissions, record
# permission filter, audit actor) works unchanged (D-200-01 bridge).
async def get_public_api_consumer(
    credentials: HTTPAuthorizationCredentials | None = Depends(public_bearer_scheme),
) -> UserResponse:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None or credentials.scheme.lower() != "bearer":
        mark_api_client_rejected("missing")
        raise credentials_exception
    token = credentials.credentials
    # Fast path: public tokens always carry the imsh_ prefix (D-02).
    # A JWT or any other token cannot match a public client.
    if not token.startswith("imsh_"):
        mark_api_client_rejected("invalid")
        raise credentials_exception

    token_hash = hashlib.sha256(token.encode()).hexdigest()

    # Redis lookup cache: key shape cache:public:token:{hash}, TTL 300s.
    # LOCKED decision (not executor discretion — see note below): the
    # cache is implemented. On miss or Redis error, fall through to DB
    # (fail-open).
    from app.db.redis import get_redis
    from redis.exceptions import RedisError
    cache_key = f"cache:public:token:{token_hash}"
    client_data = None
    try:
        redis = await get_redis()
        cached = await redis.get(cache_key)
        if cached is not None:
            try:
                client_data = json.loads(cached)
            except (ValueError, TypeError):
                # WR-05 — a corrupt/truncated cache value (JSONDecodeError is
                # a ValueError subclass) must fail open to the DB path, never
                # 500 the public auth dependency.
                client_data = None
    except (RuntimeError, RedisError):
        client_data = None

    # D-12 / D-15 fast path: a cache hit whose entry carries the full
    # service-user identity returns with ZERO DB queries and ZERO session
    # opens. Client-level revocation/expiry checks still gate the fast
    # path; a missing (old-format entry), corrupt, or inactive identity
    # falls through to the DB path (fail-open).
    if client_data is not None:
        is_revoked = client_data.get("revoked_at") is not None
        exp = client_data.get("expires_at")
        is_expired = _is_expired(exp)
        if is_revoked or is_expired:
            mark_api_client_rejected("expired" if is_expired else "invalid")
            raise credentials_exception
        identity_data = client_data.get("identity")
        if isinstance(identity_data, dict):
            try:
                # Pydantic re-coerces the JSON round-tripped fields; a
                # validation failure (corrupt identity) degrades to the
                # DB path, never a 500.
                user = UserResponse(**identity_data)
            except (ValueError, TypeError):
                user = None
            if user is not None and user.is_active:
                # Staleness bounded by the 300s token-cache TTL (D-13);
                # downstream permission checks re-validate per request.
                mark_authenticated(str(user.id))
                return user

    # Session 1: ApiClient lookup — entered ONLY on a full cache miss.
    # A cache hit without a usable identity skips this block (no session
    # opens) and goes straight to the User query below.
    if client_data is None:
        async with async_session_maker() as session:
            result = await session.execute(
                select(ApiClient).where(ApiClient.token_hash == token_hash)
            )
            api_client = result.scalar_one_or_none()
    else:
        api_client = None

    if client_data is None and api_client is None:
        mark_api_client_rejected("invalid")
        raise credentials_exception

    # Resolve effective state (from cache dict or ORM row)
    if api_client is not None:
        client_id = str(api_client.id)
        is_revoked = api_client.revoked_at is not None
        is_expired = (
            api_client.expires_at is not None and api_client.expires_at < datetime.now(timezone.utc).replace(tzinfo=None)
        )
        service_user_id = str(api_client.service_user_id)
    else:
        client_id = client_data.get("id")
        is_revoked = client_data.get("revoked_at") is not None
        exp = client_data.get("expires_at")
        is_expired = _is_expired(exp)
        service_user_id = client_data.get("service_user_id")

    if is_revoked or is_expired:
        mark_api_client_rejected("expired" if is_expired else "invalid")
        raise credentials_exception

    async with async_session_maker() as session:
        result = await session.execute(select(User).where(User.id == service_user_id))
        user = result.scalar_one_or_none()

    if user is None or not user.is_active:
        mark_api_client_rejected("invalid")
        raise credentials_exception

    # Seed the token lookup cache on DB-miss (best effort — fail-open)
    if client_data is None and api_client is not None:
        try:
            redis = await get_redis()
            await redis.set(
                cache_key,
                json.dumps({
                    "id": client_id,
                    "revoked_at": api_client.revoked_at.isoformat() if api_client.revoked_at else None,
                    "expires_at": api_client.expires_at.isoformat() if api_client.expires_at else None,
                    "service_user_id": service_user_id,
                    # D-14: identity serialized via the Pydantic response
                    # model (model_dump with mode="json" — jsonable_encoder
                    # would drop @property fields on ORM instances). Built
                    # explicitly because UserResponse.id is EscapedStr and
                    # model_validate(orm_user) rejects the UUID id value.
                    "identity": UserResponse(
                        id=str(user.id), username=user.username, email=user.email,
                        is_active=user.is_active, is_superuser=user.is_superuser
                    ).model_dump(mode="json"),
                }),
                ex=300,
            )
        except (RuntimeError, RedisError):
            pass

    mark_authenticated(str(user.id))
    return UserResponse(
        id=str(user.id), username=user.username, email=user.email,
        is_active=user.is_active, is_superuser=user.is_superuser
    )


def require_public_object_read(
    id_param: str = "object_id",
) -> Callable[..., Awaitable[UserResponse]]:
    """Public-realm read dependency for GET /object/{id} (Phase 201).

    Mirrors require_record_permission's resolution order but chains
    get_public_api_consumer and maps record-level DENY to 404 (invisible,
    READ-05) instead of the internal realm's 403.
    """
    async def _check(
        current_user: UserResponse = Depends(get_public_api_consumer),
        request: Request = None,
    ) -> UserResponse:
        # RLR-05 parity: superuser bypass — zero DB/cache queries
        if current_user.is_superuser:
            return current_user
        # Type-level: objects:read must be granted (else 403)
        user_permissions = await resolve_user_permissions(current_user.id)
        if "objects:read" not in user_permissions:
            mark_permission_denied("objects:read")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized. Missing permission: objects:read."
            )
        # Record-level: explicit DENY → 404 (invisible — READ-05 deviation)
        try:
            entity_id = uuid.UUID(request.path_params[id_param])
        except (KeyError, ValueError, TypeError, AttributeError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid or missing path parameter: {id_param}."
            )
        record_result = await resolve_record_permission(
            user_id=current_user.id,
            entity_type="object",
            record_id=entity_id,
            action="read",
        )
        if record_result is False:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Object not found"
            )
        # True or None: permissive default — type-level result stands
        return current_user
    return _check


_PUBLIC_HIERARCHY_RECORD_ENTITIES: dict[str, tuple[str, str]] = {
    "schemas": ("schema", "Schema not found"),
    "object-types": ("object_type", "Object type not found"),
}


def require_public_hierarchy_read(
    resource: str, id_param: str | None = None
) -> Callable[..., Awaitable[UserResponse]]:
    """Public-realm read gate for hierarchy entities (Phase 202, quick 261011-tv1).

    Chains get_public_api_consumer. Superuser bypass (PERM-05, zero DB/cache
    queries). Non-superusers need the {resource}:read permission else 403.
    id_param: optional path-param name to validate as a UUID (400 on invalid).
    By-id routes of record-modelled hierarchy entities (schemas, object types)
    get a record-level read step, including an ancestor DENY; a DENY returns
    the route's own 404 detail (invisible, like require_public_object_read).
    Icons and statuses have no record model, and the id-less AQL gate is
    unaffected.
    """
    async def _check(
        current_user: UserResponse = Depends(get_public_api_consumer),
        request: Request = None,
    ) -> UserResponse:
        if current_user.is_superuser:
            return current_user
        user_permissions = await resolve_user_permissions(current_user.id)
        if f"{resource}:read" not in user_permissions:
            mark_permission_denied(f"{resource}:read")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Not authorized. Missing permission: {resource}:read."
            )
        if id_param is not None:
            try:
                entity_id = uuid.UUID(request.path_params[id_param])
            except (KeyError, ValueError, TypeError, AttributeError):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid or missing path parameter: {id_param}."
                )
            record_entity = _PUBLIC_HIERARCHY_RECORD_ENTITIES.get(resource)
            if record_entity is not None:
                entity_type, not_found_detail = record_entity
                record_result = await resolve_record_permission(
                    user_id=current_user.id,
                    entity_type=entity_type,
                    record_id=entity_id,
                    action="read",
                )
                if record_result is False:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=not_found_detail
                    )
        return current_user
    return _check


def require_public_permission(resource: str, action: str) -> Callable[..., Awaitable[UserResponse]]:
    """Public-realm generic type-level gate (quick 261011-sww).

    Chains get_public_api_consumer, so a user JWT is rejected with 401.
    Superuser bypass (PERM-05, zero DB/cache queries). Non-superusers need the
    {resource}:{action} permission else 403. No record-level step.
    """
    async def _check(
        current_user: UserResponse = Depends(get_public_api_consumer),
    ) -> UserResponse:
        if current_user.is_superuser:
            return current_user
        permission_name = f"{resource}:{action}"
        user_permissions = await resolve_user_permissions(current_user.id)
        if permission_name not in user_permissions:
            mark_permission_denied(permission_name)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Not authorized. Missing permission: {permission_name}."
            )
        return current_user
    return _check


def require_public_object_create() -> Callable[..., Awaitable[UserResponse]]:
    """Public-realm create gate for POST /object/create (Phase 203).

    D-13/D-17: type-level objects:create only — no record-level step
    (the object does not exist yet). D-16: superuser bypass, zero queries.
    """
    async def _check(
        current_user: UserResponse = Depends(get_public_api_consumer),
    ) -> UserResponse:
        if current_user.is_superuser:  # D-16 — RLR-05 parity
            return current_user
        user_permissions = await resolve_user_permissions(current_user.id)
        if "objects:create" not in user_permissions:
            mark_permission_denied("objects:create")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized. Missing permission: objects:create.",
            )
        return current_user
    return _check


def require_public_object_write(
    action: str, id_param: str = "object_id",
) -> Callable[..., Awaitable[UserResponse]]:
    """Public-realm write gate for PUT/DELETE /object/{id} (Phase 203).

    D-13: type-level objects:{action} + record-level DENY → 404 (D-14,
    invisible — same detail as a nonexistent object). D-15: used via
    Depends() so it resolves before the route body. D-16: superuser
    bypass, zero queries.
    """
    async def _check(
        current_user: UserResponse = Depends(get_public_api_consumer),
        request: Request = None,
    ) -> UserResponse:
        if current_user.is_superuser:  # D-16
            return current_user
        user_permissions = await resolve_user_permissions(current_user.id)
        if f"objects:{action}" not in user_permissions:
            mark_permission_denied(f"objects:{action}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Not authorized. Missing permission: objects:{action}.",
            )
        try:
            entity_id = uuid.UUID(request.path_params[id_param])
        except (KeyError, ValueError, TypeError, AttributeError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid or missing path parameter: {id_param}.",
            )
        record_result = await resolve_record_permission(
            user_id=current_user.id,
            entity_type="object",
            record_id=entity_id,
            action=action,
        )
        if record_result is False:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Object not found",  # D-14: invisible DENY
            )
        return current_user
    return _check
