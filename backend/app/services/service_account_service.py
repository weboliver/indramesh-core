import uuid
from typing import List

from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.service_account import ServiceAccount
from app.models import Source, Target
from app.schemas.crud import ServiceAccountCreate, ServiceAccountUpdate, ServiceAccountResponse
from app.services.cache_service import (
    build_cache_key,
    build_list_cache_key,
    build_tag_key,
    get_cached,
    set_cached_with_tags,
    invalidate_entity_cache,
    CACHE_TTL_LIST,
    CACHE_TTL_ENTITY,
)
from app.services.audit_service import audit_log, model_to_dict

ENTITY_TYPE = "service_account"


async def revoke_endpoint_tokens(
    db: AsyncSession,
    *,
    source_id: uuid.UUID | None = None,
    target_id: uuid.UUID | None = None,
) -> None:
    """Clear bearer tokens bound to an endpoint whose URL changed.

    A token is only ever sent to the URL it was entered for; after a URL change
    it must be re-entered. The caller commits and invalidates the cache.
    """
    if source_id is not None:
        await db.execute(
            update(ServiceAccount)
            .where(ServiceAccount.source_id == source_id)
            .values(source_bearer_token=None)
        )
    if target_id is not None:
        await db.execute(
            update(ServiceAccount)
            .where(ServiceAccount.target_id == target_id)
            .values(target_bearer_token=None)
        )


async def create_service_account(
    db: AsyncSession, account_data: ServiceAccountCreate, actor_id: uuid.UUID
) -> ServiceAccount:
    if account_data.target_id is not None:
        result = await db.execute(
            select(Target).where(Target.id == account_data.target_id)
        )
        if not result.scalar_one_or_none():
            raise ValueError("Target not found")
    if account_data.source_id is not None:
        result = await db.execute(
            select(Source).where(Source.id == account_data.source_id)
        )
        if not result.scalar_one_or_none():
            raise ValueError("Source not found")

    db_account = ServiceAccount(
        source_id=account_data.source_id,
        target_id=account_data.target_id,
        name=account_data.name,
        username=account_data.username,
        source_bearer_token=account_data.source_bearer_token,
        target_bearer_token=account_data.target_bearer_token,
        is_active=account_data.is_active,
        expires_at=account_data.expires_at,
    )
    db.add(db_account)
    await db.flush()
    new_values = model_to_dict(db_account)
    await audit_log(db, "service_accounts", db_account.id, "CREATE", actor_id,
                    old_values=None, new_values=new_values)
    await db.commit()
    await db.refresh(db_account)
    await invalidate_entity_cache(ENTITY_TYPE)
    return db_account


async def list_service_accounts(
    db: AsyncSession,
    skip: int = 0,
    limit: int = 100,
    source_id: uuid.UUID | None = None,
    target_id: uuid.UUID | None = None,
) -> tuple[List[ServiceAccount], int]:
    cache_key = build_list_cache_key(
        ENTITY_TYPE,
        skip=skip,
        limit=limit,
        source_id=str(source_id) if source_id else None,
        target_id=str(target_id) if target_id else None,
    )
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached["items"], cached["total"]
    query = select(ServiceAccount)
    count_query = select(func.count()).select_from(ServiceAccount)
    if source_id is not None:
        query = query.where(ServiceAccount.source_id == source_id)
        count_query = count_query.where(ServiceAccount.source_id == source_id)
    if target_id is not None:
        query = query.where(ServiceAccount.target_id == target_id)
        count_query = count_query.where(ServiceAccount.target_id == target_id)
    count_result = await db.execute(count_query)
    total = count_result.scalar()
    result = await db.execute(query.offset(skip).limit(limit))
    items = result.scalars().all()
    await set_cached_with_tags(
        cache_key,
        {"items": [
            ServiceAccountResponse.model_validate(item).model_dump()
            for item in items
        ], "total": total},
        tags=[build_tag_key(ENTITY_TYPE)],
        ttl=CACHE_TTL_LIST,
    )
    return items, total


async def get_service_account(
    db: AsyncSession, account_id: uuid.UUID
) -> ServiceAccount | dict | None:
    cache_key = build_cache_key(ENTITY_TYPE, str(account_id))
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached
    result = await db.execute(
        select(ServiceAccount).where(ServiceAccount.id == account_id)
    )
    account = result.scalar_one_or_none()
    if account:
        await set_cached_with_tags(
            cache_key,
            ServiceAccountResponse.model_validate(account).model_dump(),
            tags=[build_tag_key(ENTITY_TYPE)],
            ttl=CACHE_TTL_ENTITY,
        )
    return account


async def update_service_account(
    db: AsyncSession, account_id: uuid.UUID, account_update: ServiceAccountUpdate, actor_id: uuid.UUID
) -> ServiceAccount | None:
    result = await db.execute(
        select(ServiceAccount).where(ServiceAccount.id == account_id)
    )
    account = result.scalar_one_or_none()
    if not account:
        return None

    old_values = model_to_dict(account)
    if account_update.target_id is not None:
        result = await db.execute(
            select(Target).where(Target.id == account_update.target_id)
        )
        if not result.scalar_one_or_none():
            raise ValueError("Target not found")
        account.target_id = account_update.target_id
    if account_update.source_id is not None:
        result = await db.execute(
            select(Source).where(Source.id == account_update.source_id)
        )
        if not result.scalar_one_or_none():
            raise ValueError("Source not found")
        account.source_id = account_update.source_id
    if account_update.name is not None:
        account.name = account_update.name
    if account_update.username is not None:
        account.username = account_update.username
    if account_update.source_bearer_token is not None:
        account.source_bearer_token = account_update.source_bearer_token
    if account_update.target_bearer_token is not None:
        account.target_bearer_token = account_update.target_bearer_token
    if account_update.is_active is not None:
        account.is_active = account_update.is_active
    if account_update.expires_at is not None:
        account.expires_at = account_update.expires_at

    new_values = model_to_dict(account)
    await audit_log(db, "service_accounts", account_id, "UPDATE", actor_id,
                    old_values=old_values, new_values=new_values)
    await db.commit()
    await db.refresh(account)
    await invalidate_entity_cache(ENTITY_TYPE)
    return account


async def delete_service_account(
    db: AsyncSession, account_id: uuid.UUID, actor_id: uuid.UUID
) -> bool:
    result = await db.execute(
        select(ServiceAccount).where(ServiceAccount.id == account_id)
    )
    account = result.scalar_one_or_none()
    if not account:
        return False
    old_values = model_to_dict(account)
    await audit_log(db, "service_accounts", account_id, "DELETE", actor_id,
                    old_values=old_values, new_values=None)
    await db.delete(account)
    await db.commit()
    await invalidate_entity_cache(ENTITY_TYPE)
    return True
