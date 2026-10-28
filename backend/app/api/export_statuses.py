import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.crud import (
    ExportStatusCreate, ExportStatusUpdate, ExportStatusResponse
)
from app.schemas.auth import UserResponse
from app.services.auth_service import get_current_user
from app.services.export_service import visible_export_ids_select
from app.services.export_status_service import (
    create_export_status, list_export_statuses, get_export_status as get_status_svc,
    update_export_status as update_status_svc, delete_export_status as delete_status_svc
)

router = APIRouter(prefix="/export-statuses", tags=["export-statuses"])


@router.post("/", response_model=ExportStatusResponse, status_code=status.HTTP_201_CREATED)
async def create_export_status_route(
    status_data: ExportStatusCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user)
):
    try:
        return await create_export_status(
            db,
            status_data,
            visible_export_ids=visible_export_ids_select(
                current_user.id, current_user.is_superuser
            ),
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/", response_model=List[ExportStatusResponse])
async def list_export_statuses_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum records to return"),
    response: Response = None,
):
    # T-MGK-08: an export status is export-derived data and stays inside the
    # caller's own exports; None (superuser) means no restriction.
    items, total = await list_export_statuses(
        db,
        skip=skip,
        limit=limit,
        visible_export_ids=visible_export_ids_select(
            current_user.id, current_user.is_superuser
        ),
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/{status_id}", response_model=ExportStatusResponse)
async def get_export_status_route(
    status_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user)
):
    status_obj = await get_status_svc(
        db,
        status_id,
        visible_export_ids=visible_export_ids_select(
            current_user.id, current_user.is_superuser
        ),
    )
    if not status_obj:
        raise HTTPException(status_code=404, detail="Export status not found")
    return status_obj


@router.patch("/{status_id}", response_model=ExportStatusResponse)
async def update_export_status_route(
    status_id: uuid.UUID,
    status_update: ExportStatusUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user)
):
    status_obj = await update_status_svc(
        db,
        status_id,
        status_update,
        visible_export_ids=visible_export_ids_select(
            current_user.id, current_user.is_superuser
        ),
    )
    if not status_obj:
        raise HTTPException(status_code=404, detail="Export status not found")
    return status_obj


@router.delete("/{status_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_export_status_route(
    status_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user)
):
    deleted = await delete_status_svc(
        db,
        status_id,
        visible_export_ids=visible_export_ids_select(
            current_user.id, current_user.is_superuser
        ),
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Export status not found")
    return None
