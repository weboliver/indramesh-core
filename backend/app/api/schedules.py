import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.crud import (
    ScheduleCreate, ScheduleUpdate, ScheduleResponse,
    SchedulePreviewRequest, SchedulePreviewResponse,
)
from app.schemas.auth import UserResponse
from app.services.auth_service import get_current_user, require_permission
from app.services.schedule_service import (
    create_schedule, list_schedules, get_schedule as get_schedule_svc,
    update_schedule as update_schedule_svc, delete_schedule as delete_schedule_svc,
    preview_schedule, authorize_schedule_action, can_access_schedule,
    SchedulePermissionError,
)

router = APIRouter(prefix="/schedules", tags=["schedules"])


@router.post("/", response_model=ScheduleResponse, status_code=status.HTTP_201_CREATED)
async def create_schedule_route(
    schedule_data: ScheduleCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("schedules", "create"))
):
    try:
        # Phase 289 (D-06): the authenticated creator is checked against a
        # plugin declaration's required_permission inside the service.
        return await create_schedule(db, schedule_data, current_user=current_user)
    except SchedulePermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/", response_model=List[ScheduleResponse])
async def list_schedules_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("schedules", "read")),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum records to return"),
    response: Response = None,
):
    items, total = await list_schedules(
        db,
        skip=skip,
        limit=limit,
        user_id=current_user.id,
        is_superuser=current_user.is_superuser,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.post("/preview", response_model=SchedulePreviewResponse)
async def preview_schedule_route(
    request: SchedulePreviewRequest,
    current_user: UserResponse = Depends(require_permission("schedules", "read"))
):
    try:
        return await preview_schedule(request)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/{schedule_id}", response_model=ScheduleResponse)
async def get_schedule_route(
    schedule_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("schedules", "read"))
):
    schedule = await get_schedule_svc(
        db,
        schedule_id,
        user_id=current_user.id,
        is_superuser=current_user.is_superuser,
    )
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return schedule


@router.patch("/{schedule_id}", response_model=ScheduleResponse)
async def update_schedule_route(
    schedule_id: uuid.UUID,
    schedule_update: ScheduleUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("schedules", "update"))
):
    try:
        # Phase 289 (D-06): a PATCH rechecks the merged declaration's
        # required_permission for THIS updating user.
        schedule = await update_schedule_svc(
            db, schedule_id, schedule_update, current_user=current_user,
            user_id=current_user.id, is_superuser=current_user.is_superuser,
        )
    except SchedulePermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return schedule


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule_route(
    schedule_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("schedules", "delete"))
):
    deleted = await delete_schedule_svc(
        db,
        schedule_id,
        user_id=current_user.id,
        is_superuser=current_user.is_superuser,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return None


@router.post("/{schedule_id}/run", status_code=status.HTTP_202_ACCEPTED)
async def run_schedule_now_route(
    schedule_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("schedules", "update"))
):
    """Execute a scheduled task immediately, outside the normal cron schedule.
    Uses the same template cloning logic as _execute_scheduled_job."""
    from app.services.scheduler_manager import _execute_scheduled_job
    from app.models.schedule import Schedule

    result = await db.execute(select(Schedule).where(Schedule.id == schedule_id))
    schedule = result.scalar_one_or_none()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    # 261022-np1: ownership is checked before the declaration authorization, so
    # a foreign schedule is indistinguishable from a missing one (404, never a
    # 403 that would confirm it exists).
    if not can_access_schedule(
        schedule, current_user.id, current_user.is_superuser
    ):
        raise HTTPException(status_code=404, detail="Schedule not found")

    # Phase 289 (CORESEAM-07, D-04/D-06): a manual trigger is a USER action, so
    # the current caller is reauthorized against the persisted plugin
    # declaration's required_permission before the Core callback runs. It is
    # never treated as an unattended cron authorization; the callback itself
    # mints its trusted scheduler-origin context internally.
    try:
        await authorize_schedule_action(schedule, current_user)
    except SchedulePermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))

    # _execute_scheduled_job is an async module-level function that reads the
    # Schedule from DB inside its own session. It handles template cloning,
    # task creation, and enqueueing. We trigger it but do not wait — the
    # caller gets 202 Accepted immediately with just the schedule_id.
    # The Task ID is not returned here since _execute_scheduled_job manages
    # its own session lifecycle.
    import asyncio
    asyncio.ensure_future(_execute_scheduled_job(str(schedule_id)))

    return {"status": "enqueued", "schedule_id": str(schedule_id)}
