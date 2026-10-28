import uuid
from typing import List

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.import_export_status import ExportStatus
from app.models import Export
from app.schemas.crud import ExportStatusCreate, ExportStatusUpdate
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

ENTITY_TYPE = "export_status"


def _scope(visible_export_ids) -> str:
    """A stable cache scope for the visibility sub-select (T-MGK-08).

    ``None`` means "no restriction" (system callers). Otherwise the compiled
    sub-select plus its bound parameters is deterministic per user, so one
    caller's cached page can never be served to another. ``literal_binds`` is
    avoided on purpose: the caller id arrives as a string and cannot always be
    rendered as a UUID literal.
    """
    if visible_export_ids is None:
        return "all"
    compiled = visible_export_ids.compile()
    params = sorted((key, str(value)) for key, value in compiled.params.items())
    return f"{compiled}|{params}"


async def create_export_status(
    db: AsyncSession,
    status_data: ExportStatusCreate,
    *,
    visible_export_ids=None,
) -> ExportStatus:
    export_query = select(Export).where(Export.id == status_data.export_id)
    if visible_export_ids is not None:
        # Referencing an export outside the caller's scope is indistinguishable
        # from referencing a missing one — both answer "Export not found".
        export_query = export_query.where(
            Export.id.in_(visible_export_ids)
        )
    result = await db.execute(export_query)
    export_obj = result.scalar_one_or_none()
    if not export_obj:
        raise ValueError("Export not found")

    if status_data.parent_id:
        result = await db.execute(
            select(ExportStatus).where(ExportStatus.id == status_data.parent_id)
        )
        parent = result.scalar_one_or_none()
        if not parent:
            raise ValueError("Parent export status not found")

    db_status = ExportStatus(
        export_id=status_data.export_id,
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


async def list_export_statuses(
    db: AsyncSession,
    skip: int = 0,
    limit: int = 100,
    *,
    visible_export_ids=None,
) -> tuple[List[ExportStatus], int]:
    cache_key = build_list_cache_key(
        ENTITY_TYPE, skip=skip, limit=limit, scope=_scope(visible_export_ids)
    )
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached["items"], cached["total"]

    count_query = select(func.count()).select_from(ExportStatus)
    item_query = select(ExportStatus)
    if visible_export_ids is not None:
        in_scope = ExportStatus.export_id.in_(visible_export_ids)
        count_query = count_query.where(in_scope)
        item_query = item_query.where(in_scope)

    count_result = await db.execute(count_query)
    total = count_result.scalar()
    result = await db.execute(item_query.offset(skip).limit(limit))
    items = result.scalars().all()
    await set_cached_with_tags(
        cache_key,
        {"items": jsonable_encoder(items), "total": total},
        tags=[build_tag_key(ENTITY_TYPE)],
        ttl=CACHE_TTL_LIST,
    )
    return items, total


async def get_export_status(
    db: AsyncSession,
    status_id: uuid.UUID,
    *,
    visible_export_ids=None,
) -> ExportStatus | dict | None:
    cache_key = build_cache_key(ENTITY_TYPE, str(status_id))
    if visible_export_ids is None:
        cached = await get_cached(cache_key)
        if cached is not None:
            return cached
    # A scoped caller always reads the row: the shared entity cache carries no
    # ownership information, so serving it before the scope filter would leak a
    # foreign row by id.
    query = select(ExportStatus).where(ExportStatus.id == status_id)
    if visible_export_ids is not None:
        query = query.where(ExportStatus.export_id.in_(visible_export_ids))
    result = await db.execute(query)
    status_obj = result.scalar_one_or_none()
    if status_obj:
        await set_cached_with_tags(
            cache_key,
            jsonable_encoder(status_obj),
            tags=[build_tag_key(ENTITY_TYPE)],
            ttl=CACHE_TTL_ENTITY,
        )
    return status_obj


async def update_export_status(
    db: AsyncSession,
    status_id: uuid.UUID,
    status_update: ExportStatusUpdate,
    *,
    visible_export_ids=None,
) -> ExportStatus | None:
    query = select(ExportStatus).where(ExportStatus.id == status_id)
    if visible_export_ids is not None:
        query = query.where(ExportStatus.export_id.in_(visible_export_ids))
    result = await db.execute(query)
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


async def delete_export_status(
    db: AsyncSession,
    status_id: uuid.UUID,
    *,
    visible_export_ids=None,
) -> bool:
    query = select(ExportStatus).where(ExportStatus.id == status_id)
    if visible_export_ids is not None:
        query = query.where(ExportStatus.export_id.in_(visible_export_ids))
    result = await db.execute(query)
    status_obj = result.scalar_one_or_none()
    if not status_obj:
        return False
    await db.delete(status_obj)
    await db.commit()
    await invalidate_entity_cache(ENTITY_TYPE)
    return True
