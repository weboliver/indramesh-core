import logging
import uuid
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.crud import SearchResponse, CrossEntitySearchResponse
from app.schemas.auth import UserResponse
from app.services.auth_service import require_permission
from app.services.search_service import (
    search_objects
)
from app.services.task_log_service import append_task_log


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])


@router.get("/outbox/failed")
async def list_failed_outbox_entries(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("search", "read")),
):
    """Dead-letter view for index/remove jobs the outbox_sweeper_job gave up on
    (per-entry attempts exhausted) — these need manual replay, not auto-retry."""
    from app.models.index_outbox import IndexOutbox, IndexOutboxStatus

    result = await db.execute(
        select(IndexOutbox)
        .where(IndexOutbox.status == IndexOutboxStatus.FAILED.value)
        .order_by(IndexOutbox.created_at.desc())
    )
    entries = result.scalars().all()
    return {
        "items": [
            {
                "id": str(e.id),
                "object_id": str(e.object_id),
                "object_type_id": str(e.object_type_id) if e.object_type_id else None,
                "operation": e.operation,
                "attempts": e.attempts,
                "last_error": e.last_error,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in entries
        ],
        "total": len(entries),
    }


@router.post("/outbox/{outbox_id}/retry")
async def retry_outbox_entry(
    outbox_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("search", "manage")),
):
    """Reset a dead-lettered outbox entry so the next sweeper pass re-enqueues it."""
    from app.models.index_outbox import IndexOutbox, IndexOutboxStatus

    result = await db.execute(select(IndexOutbox).where(IndexOutbox.id == outbox_id))
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outbox entry not found")

    entry.status = IndexOutboxStatus.PENDING.value
    entry.attempts = 0
    entry.last_error = None
    await db.commit()

    return {"id": str(entry.id), "status": entry.status, "message": "Entry reset — will be re-enqueued by the next sweeper pass"}


@router.get("", response_model=SearchResponse)
async def search_route(
    q: str = Query(..., min_length=1, description="Search query string"),
    object_type_id: Optional[uuid.UUID] = Query(None, description="Filter by ObjectType ID"),
    schema_id: Optional[uuid.UUID] = Query(None, description="Filter by Schema ID"),
    source_id: Optional[uuid.UUID] = Query(None, description="Filter by Source ID"),
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(20, ge=1, le=100, description="Results per page (max 100)"),
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("objects", "read")),
):
    # D-07/SEC313-15: a login alone is not enough — the route requires the
    # objects read grant, and the service restricts the query to the objects
    # this principal may see.
    return await search_objects(
        db, q, object_type_id, schema_id, source_id, page, page_size,
        user_id=str(current_user.id),
        is_superuser=current_user.is_superuser,
    )


@router.get("/cross-entity", response_model=CrossEntitySearchResponse)
async def cross_entity_search_route(
    q: str = Query(..., min_length=1, description="Search query string"),
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(5, ge=1, le=50, description="Results per entity type (max 50)"),
    current_user: UserResponse = Depends(require_permission("objects", "read")),
):
    from app.services.search_service import search_cross_entity

    # D-07: same gate as GET /search — the route returns objects alongside the
    # catalogs, and each entity group is narrowed to what this principal may
    # read inside its own query (never by dropping hits after the fact).
    results = await search_cross_entity(
        q, page=page, page_size=page_size,
        user_id=str(current_user.id),
        is_superuser=current_user.is_superuser,
    )
    return CrossEntitySearchResponse(
        query=q,
        objects=results["objects"],
        object_types=results["object_types"],
        schemas=results["schemas"],
    )


@router.post("/reindex", status_code=status.HTTP_202_ACCEPTED)
async def reindex_route(
    object_type_id: uuid.UUID = Query(..., description="ObjectType ID to reindex"),
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("search", "trigger")),
):
    # Verify the ObjectType exists
    from app.models import ObjectType as ObjectTypeModel
    from sqlalchemy import select

    result = await db.execute(
        select(ObjectTypeModel).where(ObjectTypeModel.id == object_type_id)
    )
    object_type = result.scalar_one_or_none()
    if not object_type:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"ObjectType {object_type_id} not found",
        )

    # Create an attributed Task record for tracking (D-12). The shared
    # task_service path persists the authenticated creator (plus the task audit
    # row and cache invalidation) exactly like the schema/all reindex routes —
    # the creator is never reconstructed from the queue payload.
    from app.models.task import TaskStatus, TaskStep, TaskType
    from app.schemas.crud import TaskCreate
    from app.services import task_service

    task_data = TaskCreate(
        tasktype=TaskType.REINDEX.value,
        current_step=TaskStep.SCHEMA.value,
        current_status=TaskStatus.PENDING.value,
        object_type_id=object_type_id,
    )
    new_task = await task_service.create_task(
        db, task_data, actor_id=current_user.id
    )

    # Enqueue ARQ job (ARQ pool may not exist if backend starts without worker)
    try:
        from app.db.redis import get_arq_pool
        arq_pool = await get_arq_pool()
        await arq_pool.enqueue_job(
            "bulk_reindex_job",
            str(object_type_id),
            str(new_task.id),
            # Scoped to new_task.id (fresh per call, like reindex_schema_{task.id}
            # / reindex_all_{task.id} below), not object_type_id alone — a purely
            # entity-scoped _job_id gets silently deduped by ARQ against its own
            # completed-job result cache (keep_result=86400) for any later
            # reindex trigger within 24h of the first, leaving a Task stuck at
            # PENDING forever with no indication the job never ran.
            _job_id=f"reindex_{new_task.id}",
        )
    except Exception as e:
        # If ARQ pool is not available, run inline (best-effort for dev/testing)
        logger.exception("failed to enqueue reindex job for task %s", new_task.id)
        # Update task to show it was not enqueued. Carry-03: the persisted
        # error text and the log line are safe; the raw text stays out of the DB.
        new_task.current_status = TaskStatus.FAILED.value
        new_task.error_message = task_service.safe_error_text(e, "ARQ enqueue failed")
        await append_task_log(
            db,
            new_task.id,
            logging.WARNING,
            "ARQ enqueue failed — the job was never queued",
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to enqueue reindex job: {e}",
        )

    return {
        "task_id": str(new_task.id),
        "message": "Reindex job enqueued",
        "object_type_id": str(object_type_id),
    }


@router.post("/reindex/schema", status_code=status.HTTP_202_ACCEPTED)
async def reindex_schema_route(
    schema_id: uuid.UUID = Query(..., description="Schema ID to reindex all ObjectTypes under"),
    is_permanent: bool = Query(False, description="Mark the created Task as a reusable schedule template"),
    name: str | None = Query(None, description="Optional human-readable name for a permanent Task"),
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("search", "trigger")),
):
    # Verify the Schema exists
    from app.models import Schema as SchemaModel
    from sqlalchemy import select

    result = await db.execute(
        select(SchemaModel).where(SchemaModel.id == schema_id)
    )
    schema = result.scalar_one_or_none()
    if not schema:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Schema {schema_id} not found",
        )

    # Create a Task record with REINDEX type and schema_id scope
    from app.models.task import TaskStatus, TaskStep, TaskType
    from app.schemas.crud import TaskCreate
    from app.services import task_service

    task_data = TaskCreate(
        tasktype=TaskType.REINDEX.value,
        current_step=TaskStep.SCHEMA.value,
        current_status=TaskStatus.PENDING.value,
        schema_id=schema_id,
        is_permanent=is_permanent,
        name=name,
    )
    task = await task_service.create_task(db, task_data, actor_id=current_user.id)

    # Enqueue ARQ job
    try:
        from app.db.redis import get_arq_pool
        arq_pool = await get_arq_pool()
        await arq_pool.enqueue_job(
            "schema_reindex_job",
            str(schema_id),
            str(task.id),
            _job_id=f"reindex_schema_{task.id}",
        )
    except Exception as e:
        logger.exception("failed to enqueue schema reindex job for task %s", task.id)
        task.current_status = TaskStatus.FAILED.value
        task.error_message = task_service.safe_error_text(e, "ARQ enqueue failed")
        await append_task_log(
            db,
            task.id,
            logging.WARNING,
            "ARQ enqueue failed — the job was never queued",
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to enqueue schema reindex job: {e}",
        )

    return {
        "task_id": str(task.id),
        "message": "Schema reindex job enqueued",
        "schema_id": str(schema_id),
    }


@router.post("/reindex/all", status_code=status.HTTP_202_ACCEPTED)
async def reindex_all_route(
    is_permanent: bool = Query(False, description="Mark the created Task as a reusable schedule template"),
    name: str | None = Query(None, description="Optional human-readable name for a permanent Task"),
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("search", "trigger")),
):
    from app.models.task import TaskStatus, TaskStep, TaskType
    from app.schemas.crud import TaskCreate
    from app.services import task_service

    task_data = TaskCreate(
        tasktype=TaskType.REINDEX.value,
        current_step=TaskStep.SCHEMA.value,
        current_status=TaskStatus.PENDING.value,
        is_permanent=is_permanent,
        name=name,
    )
    task = await task_service.create_task(db, task_data, actor_id=current_user.id)

    try:
        from app.db.redis import get_arq_pool
        arq_pool = await get_arq_pool()
        await arq_pool.enqueue_job(
            "reindex_all_job",
            str(task.id),
            _job_id=f"reindex_all_{task.id}",
        )
    except Exception as e:
        logger.exception("failed to enqueue reindex all job for task %s", task.id)
        task.current_status = TaskStatus.FAILED.value
        task.error_message = task_service.safe_error_text(e, "ARQ enqueue failed")
        await append_task_log(
            db,
            task.id,
            logging.WARNING,
            "ARQ enqueue failed — the job was never queued",
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to enqueue reindex all job: {e}",
        )

    return {
        "task_id": str(task.id),
        "message": "Reindex all job enqueued",
    }
