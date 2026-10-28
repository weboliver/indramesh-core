import uuid
from typing import List

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.import_export_status import ImportStatus
from app.models import Import
from app.schemas.crud import ImportStatusCreate, ImportStatusUpdate
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

ENTITY_TYPE = "import_status"


async def create_import_status(
    db: AsyncSession, status_data: ImportStatusCreate
) -> ImportStatus:
    result = await db.execute(
        select(Import).where(Import.id == status_data.import_id)
    )
    import_obj = result.scalar_one_or_none()
    if not import_obj:
        raise ValueError("Import not found")

    if status_data.parent_id:
        result = await db.execute(
            select(ImportStatus).where(ImportStatus.id == status_data.parent_id)
        )
        parent = result.scalar_one_or_none()
        if not parent:
            raise ValueError("Parent import status not found")

    db_status = ImportStatus(
        import_id=status_data.import_id,
        parent_id=status_data.parent_id,
        is_running=status_data.is_running,
        is_done=status_data.is_done,
        current_id=status_data.current_id,
        has_error=status_data.has_error,
        error_message=status_data.error_message,
        progress_percentage=status_data.progress_percentage,
    )
    db.add(db_status)
    await db.commit()
    await db.refresh(db_status)
    await invalidate_entity_cache(ENTITY_TYPE)
    return db_status


async def list_import_statuses(
    db: AsyncSession, skip: int = 0, limit: int = 100
) -> tuple[List[ImportStatus], int]:
    cache_key = build_list_cache_key(ENTITY_TYPE, skip=skip, limit=limit)
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached["items"], cached["total"]
    count_result = await db.execute(
        select(func.count()).select_from(ImportStatus)
    )
    total = count_result.scalar()
    result = await db.execute(
        select(ImportStatus).offset(skip).limit(limit)
    )
    items = result.scalars().all()
    await set_cached_with_tags(
        cache_key,
        {"items": jsonable_encoder(items), "total": total},
        tags=[build_tag_key(ENTITY_TYPE)],
        ttl=CACHE_TTL_LIST,
    )
    return items, total


async def get_import_status(
    db: AsyncSession, status_id: uuid.UUID
) -> ImportStatus | dict | None:
    cache_key = build_cache_key(ENTITY_TYPE, str(status_id))
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached
    result = await db.execute(
        select(ImportStatus).where(ImportStatus.id == status_id)
    )
    status_obj = result.scalar_one_or_none()
    if status_obj:
        await set_cached_with_tags(
            cache_key,
            jsonable_encoder(status_obj),
            tags=[build_tag_key(ENTITY_TYPE)],
            ttl=CACHE_TTL_ENTITY,
        )
    return status_obj


async def update_import_status(
    db: AsyncSession, status_id: uuid.UUID, status_update: ImportStatusUpdate
) -> ImportStatus | None:
    result = await db.execute(
        select(ImportStatus).where(ImportStatus.id == status_id)
    )
    status_obj = result.scalar_one_or_none()
    if not status_obj:
        return None

    if status_update.is_running is not None:
        status_obj.is_running = status_update.is_running
    if status_update.is_done is not None:
        status_obj.is_done = status_update.is_done
    if status_update.current_id is not None:
        status_obj.current_id = status_update.current_id
    if status_update.has_error is not None:
        status_obj.has_error = status_update.has_error
    if status_update.error_message is not None:
        status_obj.error_message = status_update.error_message
    if status_update.progress_percentage is not None:
        status_obj.progress_percentage = status_update.progress_percentage

    await db.commit()
    await db.refresh(status_obj)
    await invalidate_entity_cache(ENTITY_TYPE)
    return status_obj


async def delete_import_status(
    db: AsyncSession, status_id: uuid.UUID
) -> bool:
    result = await db.execute(
        select(ImportStatus).where(ImportStatus.id == status_id)
    )
    status_obj = result.scalar_one_or_none()
    if not status_obj:
        return False
    await db.delete(status_obj)
    await db.commit()
    await invalidate_entity_cache(ENTITY_TYPE)
    return True
