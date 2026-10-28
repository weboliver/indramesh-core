import logging
import os
import uuid
from enum import Enum
from typing import List

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.import_export import Export
from app.models.task import Task
from app.models import Schema
from app.schemas.crud import ExportCreate, ExportUpdate
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
from app.services.task_service import TERMINAL_TASK_STATUSES

logger = logging.getLogger(__name__)

ENTITY_TYPE = "export"

# CR-01: the allow-listed root every export file must resolve inside. This is the
# ONE read-side containment definition (download and delete both use
# ``resolve_export_file``). The module global is read at call time so a test (or
# a future deployment) can retarget the root by patching it.
EXPORT_ROOT = os.path.realpath(os.getenv("EXPORT_ROOT", "/tmp/exports"))


def resolve_export_file(object_key: str | None) -> str | None:
    """``object_key`` as an existing file inside ``EXPORT_ROOT``, else ``None``.

    Never raises and never returns a path that is not both contained and a real
    file: empty/None, path traversal, absolute paths elsewhere and symlink
    escapes all resolve to ``None``. Callers treat ``None`` as "no file".
    """
    if not object_key:
        return None
    resolved_path = os.path.realpath(object_key)
    if resolved_path != EXPORT_ROOT and not resolved_path.startswith(
        EXPORT_ROOT + os.sep
    ):
        return None
    if not os.path.isfile(resolved_path):
        return None
    return resolved_path


def can_access_export(
    export_obj: Export | dict, user_id, is_superuser: bool
) -> bool:
    """Ownership gate: superuser bypass, otherwise creator equality — fail closed.

    A row with ``creator_id`` NULL (scheduled export, deleted creator, legacy
    row) is superuser-only. Cached payloads are plain dicts whose ``creator_id``
    is a JSON string, hence the string comparison against either shape.
    """
    if is_superuser:
        return True
    if isinstance(export_obj, dict):
        creator_id = export_obj.get("creator_id")
    else:
        creator_id = export_obj.creator_id
    if creator_id is None:
        return False
    return str(creator_id) == str(user_id)


def visible_export_ids_select(user_id, is_superuser: bool):
    """Ids of the exports a caller may see, or ``None`` meaning "no restriction".

    ``None`` is returned only for a superuser. Every other caller gets a
    sub-select used both as a SQL ``IN`` predicate and as the argument for
    ``Task.export_id.in_(...)``-style scoping; a non-superuser without a user id
    would produce an empty result on purpose (nothing is theirs).
    """
    if is_superuser:
        return None
    return select(Export.id).where(Export.creator_id == user_id)


class ExportDeleteOutcome(str, Enum):
    """The three answers ``delete_export`` gives its route."""

    DELETED = "deleted"
    NOT_FOUND = "not_found"
    ACTIVE = "active"


async def create_export(
    db: AsyncSession,
    export_data: ExportCreate,
    *,
    creator_id: uuid.UUID | None = None,
) -> Export:
    result = await db.execute(
        select(Schema).where(Schema.id == export_data.schema_id)
    )
    schema = result.scalar_one_or_none()
    if not schema:
        raise ValueError("Schema not found")

    db_export = Export(
        schema_id=export_data.schema_id,
        # object_key is server-authored: a fresh export never points at a file,
        # and no request schema carries the field (see crud.ExportCreate).
        object_key=None,
        creator_id=creator_id,
        error_code=export_data.error_code,
        error_message=export_data.error_message,
        status=export_data.status,
        total_objects=export_data.total_objects,
        processed_objects=export_data.processed_objects,
    )
    db.add(db_export)
    await db.commit()
    await db.refresh(db_export)
    await invalidate_entity_cache(ENTITY_TYPE)
    return db_export


async def list_exports(
    db: AsyncSession,
    skip: int = 0,
    limit: int = 100,
    *,
    user_id=None,
    is_superuser: bool = False,
) -> tuple[List[Export], int]:
    # T-MGK-04: the cache key is scoped to the caller, so a cached page can never
    # be served to a different user. invalidate_entity_cache("export") clears
    # every key registered under the shared tag, i.e. every scope.
    scope = "superuser" if is_superuser else str(user_id)
    cache_key = build_list_cache_key(ENTITY_TYPE, skip=skip, limit=limit, scope=scope)
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached["items"], cached["total"]

    if not is_superuser and user_id is None:
        # No actor ⇒ nothing is owned ⇒ no query at all.
        return [], 0

    count_query = select(func.count()).select_from(Export)
    item_query = select(Export).offset(skip).limit(limit)
    if not is_superuser:
        count_query = count_query.where(Export.creator_id == user_id)
        item_query = select(Export).where(Export.creator_id == user_id).offset(skip).limit(limit)

    count_result = await db.execute(count_query)
    total = count_result.scalar()
    result = await db.execute(item_query)
    items = result.scalars().all()
    await set_cached_with_tags(
        cache_key,
        {"items": jsonable_encoder(items), "total": total},
        tags=[build_tag_key(ENTITY_TYPE)],
        ttl=CACHE_TTL_LIST,
    )
    return items, total


async def get_export(
    db: AsyncSession, export_id: uuid.UUID
) -> Export | dict | None:
    cache_key = build_cache_key(ENTITY_TYPE, str(export_id))
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached
    result = await db.execute(
        select(Export).where(Export.id == export_id)
    )
    export_obj = result.scalar_one_or_none()
    if export_obj:
        await set_cached_with_tags(
            cache_key,
            jsonable_encoder(export_obj),
            tags=[build_tag_key(ENTITY_TYPE)],
            ttl=CACHE_TTL_ENTITY,
        )
    return export_obj


async def update_export(
    db: AsyncSession,
    export_id: uuid.UUID,
    export_update: ExportUpdate,
    *,
    object_key: str | None = None,
) -> Export | None:
    """Apply an update; ``object_key`` is a system-only writer keyword.

    The CSV path is written exclusively by ``worker.process_export`` and
    ``export_execution_service.execute_csv_export`` through this keyword. No
    request schema carries ``object_key`` any more, so a route must never pass
    it — doing so would let a client redirect the row at another user's file.
    """
    result = await db.execute(
        select(Export).where(Export.id == export_id)
    )
    export_obj = result.scalar_one_or_none()
    if not export_obj:
        return None

    if object_key is not None:
        export_obj.object_key = object_key
    if export_update.error_code is not None:
        export_obj.error_code = export_update.error_code
    if export_update.error_message is not None:
        export_obj.error_message = export_update.error_message
    if export_update.status is not None:
        export_obj.status = export_update.status
    if export_update.total_objects is not None:
        export_obj.total_objects = export_update.total_objects
    if export_update.processed_objects is not None:
        export_obj.processed_objects = export_update.processed_objects

    await db.commit()
    await db.refresh(export_obj)
    await invalidate_entity_cache(ENTITY_TYPE)
    return export_obj


async def delete_export(
    db: AsyncSession,
    export_id: uuid.UUID,
    *,
    user_id=None,
    is_superuser: bool = False,
) -> ExportDeleteOutcome:
    """Remove a terminal export run: the DB row and its file, in that order.

    Route-only (no worker caller). Three outcomes keep the route mapping honest:
    a missing row and a foreign row are the same ``NOT_FOUND`` (no existence
    oracle), a run with a non-terminal linked Task is ``ACTIVE``, and only a
    terminal, accessible run is ``DELETED``. The path is resolved through
    ``resolve_export_file`` BEFORE the row is gone, so the file is unlinked only
    when it really resolves inside ``EXPORT_ROOT``; a warning names the export id
    and never the path or filename.
    """
    result = await db.execute(
        select(Export).where(Export.id == export_id)
    )
    export_obj = result.scalar_one_or_none()
    if not export_obj or not can_access_export(export_obj, user_id, is_superuser):
        return ExportDeleteOutcome.NOT_FOUND

    active_task = (
        await db.execute(
            select(Task.id).where(
                Task.export_id == export_id,
                Task.current_status.notin_(list(TERMINAL_TASK_STATUSES)),
            )
        )
    ).first()
    if active_task is not None:
        return ExportDeleteOutcome.ACTIVE

    resolved_path = resolve_export_file(export_obj.object_key)

    # Task.export_id has ondelete="CASCADE" — the DB removes any dependent
    # Task rows automatically when export_obj is deleted below; no need to
    # fetch and delete them here ourselves.
    await db.delete(export_obj)
    await db.commit()
    await invalidate_entity_cache(ENTITY_TYPE)

    if resolved_path:
        try:
            os.remove(resolved_path)
        except OSError:
            logger.warning("export %s: could not remove its file", export_id)
    return ExportDeleteOutcome.DELETED
