import base64
import hashlib
import secrets
import uuid
from datetime import datetime, timezone
from typing import List

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiClient, User, Role, UserRole
from app.schemas.api_client import ApiClientCreate, ApiClientUpdate
from app.services.auth_service import get_password_hash
from app.services.cache_service import invalidate_user_permissions_cache
from app.services.audit_service import audit_log, model_to_dict

ENTITY_TYPE = "api_client"


def generate_client_token() -> str:
    # D-02 — imsh_ + 32 random bytes base64url (~44 chars total)
    return "imsh_" + base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def _hash_token(token: str) -> str:
    # D-01 — SHA-256 at rest, never Fernet
    return hashlib.sha256(token.encode()).hexdigest()


async def create_api_client(
    db: AsyncSession, client_data: ApiClientCreate, actor_id: uuid.UUID
) -> tuple[ApiClient, str]:
    raw_token = generate_client_token()
    token_hash = _hash_token(raw_token)

    # D-05 — synthetic service user (real User row)
    username = f"svc-{client_data.name[:50].lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}"
    service_user = User(
        username=username,
        email=f"{username}@indramesh.local",
        password_hash=get_password_hash(secrets.token_urlsafe(32)),  # unguessable, never used for login
        is_active=True,
        is_service_account=True,
    )
    db.add(service_user)
    await db.flush()

    # D-07 — default Viewer role (least privilege)
    role_result = await db.execute(select(Role).where(Role.name == "Viewer"))
    viewer_role = role_result.scalar_one_or_none()
    if viewer_role is not None:
        db.add(UserRole(user_id=service_user.id, role_id=viewer_role.id))
        await db.flush()

    db_client = ApiClient(
        name=client_data.name,
        token_hash=token_hash,
        token_prefix=raw_token[:12],  # D-03 — imsh_ (5) + first 7 secret chars
        expires_at=client_data.expires_at,
        service_user_id=service_user.id,
    )
    db.add(db_client)
    await db.flush()
    new_values = model_to_dict(db_client)
    await audit_log(db, "api_clients", db_client.id, "CREATE", actor_id,
                    old_values=None, new_values=new_values)
    await db.commit()
    await db.refresh(db_client)
    return db_client, raw_token


async def list_api_clients(
    db: AsyncSession, skip: int = 0, limit: int = 100
) -> tuple[List[ApiClient], int]:
    # No cache needed — admin surface, low volume
    query = select(ApiClient)
    count_query = select(func.count()).select_from(ApiClient)
    count_result = await db.execute(count_query)
    total = count_result.scalar()
    result = await db.execute(query.offset(skip).limit(limit))
    items = result.scalars().all()
    return items, total


async def get_api_client(db: AsyncSession, client_id: uuid.UUID) -> ApiClient | None:
    result = await db.execute(select(ApiClient).where(ApiClient.id == client_id))
    return result.scalar_one_or_none()


async def update_api_client(
    db: AsyncSession, client_id: uuid.UUID, update: ApiClientUpdate, actor_id: uuid.UUID
) -> ApiClient | None:
    result = await db.execute(select(ApiClient).where(ApiClient.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        return None

    old_values = model_to_dict(client)
    # D-04 / T-200-15 — never allow token/revocation changes via this path;
    # revocation is its own explicit endpoint (POST /{id}/revoke).
    if update.name is not None:
        client.name = update.name
    if update.expires_at is not None:
        client.expires_at = update.expires_at

    new_values = model_to_dict(client)
    await audit_log(db, "api_clients", client_id, "UPDATE", actor_id,
                    old_values=old_values, new_values=new_values)
    await db.commit()
    await db.refresh(client)

    # WR-01 — an expiry change must take effect immediately: purge the
    # token-lookup cache key written by get_public_api_consumer so force-
    # expiry (and expiry-extension) are seen on the very next request.
    # Mirrors revoke_api_client's fail-open pattern; only the token cache
    # needs purging here — the permission cache is unaffected by an expiry
    # change. Runs after commit so the DB state is authoritative first.
    if update.expires_at is not None:
        try:
            from app.db.redis import get_redis
            from redis.exceptions import RedisError

            redis = await get_redis()
            await redis.delete(f"cache:public:token:{client.token_hash}")
        except (RuntimeError, RedisError):
            pass
    return client


async def revoke_api_client(
    db: AsyncSession, client_id: uuid.UUID, actor_id: uuid.UUID
) -> ApiClient | None:
    result = await db.execute(select(ApiClient).where(ApiClient.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        return None

    old_values = model_to_dict(client)
    # D-08 — revoked_at + is_active=False stop auth and privilege resolution
    # instantly, independent of cache state.
    client.revoked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.flush()

    user_result = await db.execute(select(User).where(User.id == client.service_user_id))
    service_user = user_result.scalar_one_or_none()
    if service_user is not None:
        service_user.is_active = False

    # Redis purge — BOTH cache types are always implemented (locked in
    # Plan 03, not conditional): (a) the token-lookup cache key written by
    # get_public_api_consumer (D-08a) and (b) the service user's
    # permission-cache keys (D-08b). Fail-open on Redis outage: the DB
    # state change above is never blocked by a cache failure.
    try:
        from app.db.redis import get_redis
        from redis.exceptions import RedisError

        redis = await get_redis()
        await redis.delete(f"cache:public:token:{client.token_hash}")  # D-08a
        await invalidate_user_permissions_cache([str(client.service_user_id)])  # D-08b
    except (RuntimeError, RedisError):
        pass

    new_values = model_to_dict(client)
    await audit_log(db, "api_clients", client_id, "REVOKE", actor_id,
                    old_values=old_values, new_values=new_values)
    await db.commit()
    await db.refresh(client)
    return client


class ApiClientNotRevokedError(Exception):
    """Raised when deletion is attempted on a client that hasn't been revoked yet."""


async def delete_api_client(
    db: AsyncSession, client_id: uuid.UUID, actor_id: uuid.UUID
) -> ApiClient | None:
    result = await db.execute(select(ApiClient).where(ApiClient.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        return None

    # Hard-delete is only allowed once a client is revoked — forces the
    # instant auth/permission-cache purge in revoke_api_client() to have
    # already happened before the row (and its token_hash) disappears, so
    # a still-valid token can never be silently deleted out from under an
    # active integration.
    if client.revoked_at is None:
        raise ApiClientNotRevokedError()

    old_values = model_to_dict(client)
    await audit_log(db, "api_clients", client_id, "DELETE", actor_id,
                    old_values=old_values, new_values=None)
    await db.delete(client)
    await db.commit()
    return client
