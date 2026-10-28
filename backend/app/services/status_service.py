import uuid
from typing import List

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Status, Schema
from app.schemas.crud import StatusCreate, StatusUpdate, StatusResponse
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
from app.services.translation_service import resolve_translations, delete_translations_for

ENTITY_TYPE = "status"


async def create_status(
    db: AsyncSession,
    status_data: StatusCreate,
    actor_id: uuid.UUID,
    *,
    id: uuid.UUID | None = None,
) -> Status:
    result = await db.execute(
        select(Schema).where(Schema.id == status_data.schema_id)
    )
    schema = result.scalar_one_or_none()
    if not schema:
        raise ValueError("Schema not found")

    db_status = Status(
        id=id or uuid.uuid4(),
        schema_id=status_data.schema_id,
        name=status_data.name,
        color=status_data.color,
        order=status_data.order,
        status_category=status_data.status_category,
    )
    db.add(db_status)
    await db.flush()
    new_values = model_to_dict(db_status)
    await audit_log(db, "statuses", db_status.id, "CREATE", actor_id,
                    old_values=None, new_values=new_values)
    await db.commit()
    await db.refresh(db_status)
    await invalidate_entity_cache(ENTITY_TYPE)
    return db_status


async def list_statuses(
    db: AsyncSession, skip: int = 0, limit: int = 100, schema_id: uuid.UUID | None = None,
    lang: str | None = None,
) -> tuple[List[Status], int]:
    # schema_id was silently ignored here despite the frontend always sending
    # it (use-other-resources.js's useStatuses()) — every schema's Status tab
    # showed EVERY status across ALL schemas instead of just its own
    # (confirmed live, 2026-07-27).
    cache_key = build_list_cache_key(
        ENTITY_TYPE, skip=skip, limit=limit,
        schema_id=str(schema_id) if schema_id else None,
        lang=lang,
    )
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached["items"], cached["total"]
    count_query = select(func.count()).select_from(Status)
    query = select(Status).offset(skip).limit(limit)
    if schema_id is not None:
        count_query = count_query.where(Status.schema_id == schema_id)
        query = query.where(Status.schema_id == schema_id)
    count_result = await db.execute(count_query)
    total = count_result.scalar()
    result = await db.execute(query)
    items = result.scalars().all()
    serialized_items = [StatusResponse.model_validate(item).model_dump() for item in items]
    if lang:
        # D-05/D-06: strict D-06 when schema scope is known (schema-filtered list);
        # schema_id=None resolves per-entity (unfiltered list, no list-wide 400).
        resolved = await resolve_translations(
            db, "status",
            [item.id for item in items],
            ["name"],
            lang,
            schema_id=schema_id,
        )
        for item in serialized_items:
            entity_resolution = resolved.get(item["id"], {})
            if "name" in entity_resolution:
                value, serving = entity_resolution["name"]
                item["name"] = value
                item["resolved_language"] = serving
    await set_cached_with_tags(
        cache_key,
        {"items": serialized_items, "total": total},
        tags=[build_tag_key(ENTITY_TYPE)],
        ttl=CACHE_TTL_LIST,
    )
    if lang:
        return serialized_items, total
    return items, total


async def get_status(
    db: AsyncSession, status_id: uuid.UUID, lang: str | None = None
) -> Status | dict | None:
    cache_key = build_cache_key(
        ENTITY_TYPE, f"{status_id}:{lang}" if lang else str(status_id)
    )
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached
    result = await db.execute(
        select(Status).where(Status.id == status_id)
    )
    status_obj = result.scalar_one_or_none()
    if status_obj:
        serialized = StatusResponse.model_validate(status_obj).model_dump()
        if lang:
            # D-06 strict: resolve via the entity's own schema scope (Status.schema_id is NOT NULL).
            resolved = await resolve_translations(
                db, "status", [status_id], ["name"],
                lang, schema_id=status_obj.schema_id,
            )
            entity_resolution = resolved.get(status_id, {})
            if "name" in entity_resolution:
                value, serving = entity_resolution["name"]
                serialized["name"] = value
                serialized["resolved_language"] = serving
        await set_cached_with_tags(
            cache_key,
            serialized,
            tags=[build_tag_key(ENTITY_TYPE)],
            ttl=CACHE_TTL_ENTITY,
        )
    if lang:
        return serialized if status_obj else None
    return status_obj


async def update_status(
    db: AsyncSession, status_id: uuid.UUID, status_update: StatusUpdate, actor_id: uuid.UUID
) -> Status | None:
    result = await db.execute(
        select(Status).where(Status.id == status_id)
    )
    status_obj = result.scalar_one_or_none()
    if not status_obj:
        return None

    old_values = model_to_dict(status_obj)
    if status_update.schema_id is not None:
        result = await db.execute(
            select(Schema).where(Schema.id == status_update.schema_id)
        )
        if not result.scalar_one_or_none():
            raise ValueError("Schema not found")
        status_obj.schema_id = status_update.schema_id
    if status_update.name is not None:
        status_obj.name = status_update.name
    if status_update.color is not None:
        status_obj.color = status_update.color
    if status_update.order is not None:
        status_obj.order = status_update.order
    if status_update.status_category is not None:
        status_obj.status_category = status_update.status_category

    new_values = model_to_dict(status_obj)
    await audit_log(db, "statuses", status_id, "UPDATE", actor_id,
                    old_values=old_values, new_values=new_values)
    await db.commit()
    await db.refresh(status_obj)
    await invalidate_entity_cache(ENTITY_TYPE)
    return status_obj


async def delete_status(
    db: AsyncSession, status_id: uuid.UUID, actor_id: uuid.UUID
) -> bool:
    result = await db.execute(
        select(Status).where(Status.id == status_id)
    )
    status_obj = result.scalar_one_or_none()
    if not status_obj:
        return False
    old_values = model_to_dict(status_obj)
    await audit_log(db, "statuses", status_id, "DELETE", actor_id,
                    old_values=old_values, new_values=None)
    # D-15 / MULTI-07: purge the polymorphic translation rows in the SAME
    # transaction as the entity delete — no orphans.
    await delete_translations_for(db, "status", [status_id])
    await db.delete(status_obj)
    await db.commit()
    await invalidate_entity_cache(ENTITY_TYPE)
    return True
