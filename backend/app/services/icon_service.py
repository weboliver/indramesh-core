import uuid
from typing import List

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Icon, ObjectType
from app.schemas.crud import IconCreate, IconUpdate
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
from app.public_api.objecttype_schema_cache import purge_public_objecttype_schema_cache

ENTITY_TYPE = "icon"


async def _purge_types_using_icon(db: AsyncSession, icon_id: uuid.UUID) -> None:
    """Purge the type-schema tier for every object type using this icon.

    icon_id is a direct column on object_types — no descendant expansion
    (D-05 applies to inherited attribute definitions, not icons). Best-effort:
    the purge helper is fail-open by construction (D-03/D-04).
    """
    result = await db.execute(
        select(ObjectType.id).where(ObjectType.icon_id == icon_id)
    )
    type_ids = [str(tid) for tid in result.scalars().all()]
    if type_ids:
        await purge_public_objecttype_schema_cache(type_ids)


async def create_icon(
    db: AsyncSession, icon_data: IconCreate, actor_id: uuid.UUID
) -> Icon:
    db_icon = Icon(
        schema_id=icon_data.schema_id,
        name=icon_data.name,
        local_name=icon_data.local_name,
        url=icon_data.url,
        type=icon_data.type,
    )
    db.add(db_icon)
    await db.flush()
    new_values = model_to_dict(db_icon)
    await audit_log(db, "icons", db_icon.id, "CREATE", actor_id,
                    old_values=None, new_values=new_values)
    await db.commit()
    await db.refresh(db_icon)
    await invalidate_entity_cache(ENTITY_TYPE)
    await _purge_types_using_icon(db, db_icon.id)
    return db_icon


async def list_icons(
    db: AsyncSession, skip: int = 0, limit: int = 100
) -> tuple[List[Icon], int]:
    cache_key = build_list_cache_key(ENTITY_TYPE, skip=skip, limit=limit)
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached["items"], cached["total"]
    count_result = await db.execute(
        select(func.count()).select_from(Icon)
    )
    total = count_result.scalar()
    result = await db.execute(
        select(Icon).offset(skip).limit(limit)
    )
    items = result.scalars().all()
    await set_cached_with_tags(
        cache_key,
        {"items": jsonable_encoder(items), "total": total},
        tags=[build_tag_key(ENTITY_TYPE)],
        ttl=CACHE_TTL_LIST,
    )
    return items, total


async def get_icon(
    db: AsyncSession, icon_id: uuid.UUID
) -> Icon | dict | None:
    cache_key = build_cache_key(ENTITY_TYPE, str(icon_id))
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached
    result = await db.execute(
        select(Icon).where(Icon.id == icon_id)
    )
    icon = result.scalar_one_or_none()
    if icon:
        await set_cached_with_tags(
            cache_key,
            jsonable_encoder(icon),
            tags=[build_tag_key(ENTITY_TYPE)],
            ttl=CACHE_TTL_ENTITY,
        )
    return icon


async def update_icon(
    db: AsyncSession,
    icon_id: uuid.UUID,
    icon_update: IconUpdate,
    actor_id: uuid.UUID,
) -> Icon | None:
    result = await db.execute(
        select(Icon).where(Icon.id == icon_id)
    )
    icon = result.scalar_one_or_none()
    if not icon:
        return None

    old_values = model_to_dict(icon)
    if icon_update.schema_id is not None:
        icon.schema_id = icon_update.schema_id
    if icon_update.name is not None:
        icon.name = icon_update.name
    if icon_update.local_name is not None:
        icon.local_name = icon_update.local_name
    if icon_update.url is not None:
        icon.url = icon_update.url
    if icon_update.type is not None:
        icon.type = icon_update.type

    new_values = model_to_dict(icon)
    await audit_log(db, "icons", icon_id, "UPDATE", actor_id,
                    old_values=old_values, new_values=new_values)
    await db.commit()
    await db.refresh(icon)
    await invalidate_entity_cache(ENTITY_TYPE)
    await _purge_types_using_icon(db, icon_id)
    return icon


async def delete_icon(
    db: AsyncSession, icon_id: uuid.UUID, actor_id: uuid.UUID
) -> bool:
    result = await db.execute(
        select(Icon).where(Icon.id == icon_id)
    )
    icon = result.scalar_one_or_none()
    if not icon:
        return False
    old_values = model_to_dict(icon)
    await audit_log(db, "icons", icon_id, "DELETE", actor_id,
                    old_values=old_values, new_values=None)
    await db.delete(icon)
    await db.commit()
    await invalidate_entity_cache(ENTITY_TYPE)
    await _purge_types_using_icon(db, icon_id)
    return True
