import uuid
from typing import List

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.import_export import Import
from app.models import Schema
from app.schemas.crud import ImportCreate, ImportUpdate
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

ENTITY_TYPE = "import"


async def create_import(
    db: AsyncSession, import_data: ImportCreate
) -> Import:
    result = await db.execute(
        select(Schema).where(Schema.id == import_data.schema_id)
    )
    schema = result.scalar_one_or_none()
    if not schema:
        raise ValueError("Schema not found")

    db_import = Import(
        schema_id=import_data.schema_id,
        object_key=import_data.object_key,
        error_code=import_data.error_code,
        error_message=import_data.error_message,
        status=import_data.status,
        total_objects=import_data.total_objects,
        processed_objects=import_data.processed_objects,
    )
    db.add(db_import)
    await db.commit()
    await db.refresh(db_import)
    await invalidate_entity_cache(ENTITY_TYPE)
    return db_import


async def list_imports(
    db: AsyncSession, skip: int = 0, limit: int = 100
) -> tuple[List[Import], int]:
    cache_key = build_list_cache_key(ENTITY_TYPE, skip=skip, limit=limit)
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached["items"], cached["total"]
    count_result = await db.execute(
        select(func.count()).select_from(Import)
    )
    total = count_result.scalar()
    result = await db.execute(
        select(Import).offset(skip).limit(limit)
    )
    items = result.scalars().all()
    await set_cached_with_tags(
        cache_key,
        {"items": jsonable_encoder(items), "total": total},
        tags=[build_tag_key(ENTITY_TYPE)],
        ttl=CACHE_TTL_LIST,
    )
    return items, total


async def get_import(
    db: AsyncSession, import_id: uuid.UUID
) -> Import | dict | None:
    cache_key = build_cache_key(ENTITY_TYPE, str(import_id))
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached
    result = await db.execute(
        select(Import).where(Import.id == import_id)
    )
    import_obj = result.scalar_one_or_none()
    if import_obj:
        await set_cached_with_tags(
            cache_key,
            jsonable_encoder(import_obj),
            tags=[build_tag_key(ENTITY_TYPE)],
            ttl=CACHE_TTL_ENTITY,
        )
    return import_obj


async def update_import(
    db: AsyncSession, import_id: uuid.UUID, import_update: ImportUpdate
) -> Import | None:
    result = await db.execute(
        select(Import).where(Import.id == import_id)
    )
    import_obj = result.scalar_one_or_none()
    if not import_obj:
        return None

    if import_update.object_key is not None:
        import_obj.object_key = import_update.object_key
    if import_update.error_code is not None:
        import_obj.error_code = import_update.error_code
    if import_update.error_message is not None:
        import_obj.error_message = import_update.error_message
    if import_update.status is not None:
        import_obj.status = import_update.status
    if import_update.total_objects is not None:
        import_obj.total_objects = import_update.total_objects
    if import_update.processed_objects is not None:
        import_obj.processed_objects = import_update.processed_objects

    await db.commit()
    await db.refresh(import_obj)
    await invalidate_entity_cache(ENTITY_TYPE)
    return import_obj


async def delete_import(
    db: AsyncSession, import_id: uuid.UUID
) -> bool:
    result = await db.execute(
        select(Import).where(Import.id == import_id)
    )
    import_obj = result.scalar_one_or_none()
    if not import_obj:
        return False
    # Task.import_id has ondelete="CASCADE" — the DB removes any dependent
    # Task rows automatically when import_obj is deleted below; no need to
    # fetch and delete them here ourselves.
    await db.delete(import_obj)
    await db.commit()
    await invalidate_entity_cache(ENTITY_TYPE)
    return True
