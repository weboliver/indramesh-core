import logging
import os
import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.db.redis import get_arq_pool
from app.models.import_export import Export
from app.models.task import TaskType, TaskStep, TaskStatus
from app.schemas.crud import (
    ExportCreate, ExportUpdate, ExportResponse, TaskCreate
)
from app.schemas.auth import UserResponse
from app.services.auth_service import get_current_user, require_permission
from app.services.export_service import (
    create_export, list_exports, get_export as get_export_svc,
    update_export as update_export_svc, delete_export as delete_export_svc,
    can_access_export, resolve_export_file, ExportDeleteOutcome,
)
from app.services.task_service import create_task as create_task_svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/exports", tags=["exports"])


@router.post("/", response_model=ExportResponse, status_code=status.HTTP_201_CREATED)
async def create_export_route(
    export_data: ExportCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("exports", "create"))
):
    # EXT-10-T1: an export without a definition used to persist the Export, create
    # the Task and enqueue process_export, whose worker branch then walked Task
    # steps and reported SUCCESS/COMPLETED without ever writing a file. Refuse
    # before any Export/Task write or enqueue. The status is 400 (not 422) because
    # the schema field stays Optional on purpose: the request-validation handler
    # maps every RequestValidationError to 422, and D-17 locked the answer to 400.
    if not export_data.export_definition_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="export_definition_id is required",
        )

    try:
        # The acting user owns the export — including a superuser, whose row is
        # then attributable instead of falling into the creatorless bucket.
        export_obj = await create_export(
            db, export_data, creator_id=current_user.id
        )

        task_data = TaskCreate(
            export_id=export_obj.id,
            current_step=TaskStep.SCHEMA.value,
            current_status=TaskStatus.PENDING.value,
            tasktype=TaskType.EXPORT.value,
            export_definition_id=export_data.export_definition_id,
        )
        task = await create_task_svc(db, task_data, actor_id=current_user.id)

        try:
            arq_pool = await get_arq_pool()
            await arq_pool.enqueue_job(
                "process_export",
                str(task.id),
                str(export_data.export_definition_id),
                _job_id=f"export_{task.id}",
            )
        except Exception:
            logger.exception("failed to enqueue export job for task %s", task.id)

        return export_obj
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/", response_model=List[ExportResponse])
async def list_exports_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("exports", "read")),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum records to return"),
    response: Response = None,
):
    items, total = await list_exports(
        db, skip=skip, limit=limit,
        user_id=current_user.id, is_superuser=current_user.is_superuser,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/{export_id}", response_model=ExportResponse)
async def get_export_route(
    export_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("exports", "read"))
):
    export_obj = await get_export_svc(db, export_id)
    # T-MGK-05: a foreign row is indistinguishable from a missing one.
    if not export_obj or not can_access_export(
        export_obj, current_user.id, current_user.is_superuser
    ):
        raise HTTPException(status_code=404, detail="Export not found")
    return export_obj


@router.patch("/{export_id}", response_model=ExportResponse)
async def update_export_route(
    export_id: uuid.UUID,
    export_update: ExportUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("exports", "update"))
):
    # The gate lives here, not in update_export: the worker and the CSV executor
    # call the service directly to persist the server-authored file path.
    export_obj = await get_export_svc(db, export_id)
    if not export_obj or not can_access_export(
        export_obj, current_user.id, current_user.is_superuser
    ):
        raise HTTPException(status_code=404, detail="Export not found")
    updated = await update_export_svc(db, export_id, export_update)
    if not updated:
        raise HTTPException(status_code=404, detail="Export not found")
    return updated


@router.delete("/{export_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_export_route(
    export_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("exports", "delete"))
):
    outcome = await delete_export_svc(
        db, export_id,
        user_id=current_user.id, is_superuser=current_user.is_superuser,
    )
    if outcome == ExportDeleteOutcome.NOT_FOUND:
        # Ownership mismatch and a missing row are deliberately indistinguishable.
        raise HTTPException(status_code=404, detail="Export not found")
    if outcome == ExportDeleteOutcome.ACTIVE:
        # A non-terminal linked Task still owns the run; both stay.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Export has an active task and cannot be deleted",
        )
    return None


@router.get("/csv/{export_id}/download")
async def download_csv_export(
    export_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("exports", "read"))
):
    # Direct DB lookup — intentionally bypasses export_service.get_export's cache
    # layer: a cache hit there returns a plain dict (no .object_key attribute),
    # and this endpoint needs a guaranteed-fresh, typed ORM row for file I/O.
    result = await db.execute(select(Export).where(Export.id == export_id))
    export_obj = result.scalar_one_or_none()
    # Ownership is decided BEFORE any object_key/file branch so a foreign row
    # never reveals whether a file exists.
    if not export_obj or not can_access_export(
        export_obj, current_user.id, current_user.is_superuser
    ):
        raise HTTPException(status_code=404, detail="Export not found")

    if not export_obj.object_key:
        raise HTTPException(status_code=404, detail="Export file not available")

    # CR-01: containment check — reject anything that resolves outside EXPORT_ROOT
    # (directory traversal, absolute paths to arbitrary files, symlink escapes, etc.)
    # before ever touching the filesystem.
    resolved_path = resolve_export_file(export_obj.object_key)
    if resolved_path is None:
        raise HTTPException(status_code=404, detail="Export file not found on disk")

    filename = os.path.basename(resolved_path)

    def iterfile():
        with open(resolved_path, mode="rb") as f:
            yield from f

    return StreamingResponse(
        iterfile(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
