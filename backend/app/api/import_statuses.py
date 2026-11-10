import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.crud import (
    ImportStatusCreate, ImportStatusUpdate, ImportStatusResponse
)
from app.schemas.auth import UserResponse
from app.services.auth_service import require_permission
from app.services.import_status_service import (
    create_import_status, list_import_statuses, get_import_status as get_status_svc,
    update_import_status as update_status_svc, delete_import_status as delete_status_svc
)

router = APIRouter(prefix="/import-statuses", tags=["import-statuses"])


@router.post("/", response_model=ImportStatusResponse, status_code=status.HTTP_201_CREATED)
async def create_import_status_route(
    status_data: ImportStatusCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("imports", "create"))
):
    try:
        return await create_import_status(db, status_data)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/", response_model=List[ImportStatusResponse])
async def list_import_statuses_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("imports", "read")),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum records to return"),
    response: Response = None,
):
    items, total = await list_import_statuses(db, skip=skip, limit=limit)
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/{status_id}", response_model=ImportStatusResponse)
async def get_import_status_route(
    status_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("imports", "read"))
):
    status_obj = await get_status_svc(db, status_id)
    if not status_obj:
        raise HTTPException(status_code=404, detail="Import status not found")
    return status_obj


@router.patch("/{status_id}", response_model=ImportStatusResponse)
async def update_import_status_route(
    status_id: uuid.UUID,
    status_update: ImportStatusUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("imports", "update"))
):
    status_obj = await update_status_svc(db, status_id, status_update)
    if not status_obj:
        raise HTTPException(status_code=404, detail="Import status not found")
    return status_obj


@router.delete("/{status_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_import_status_route(
    status_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("imports", "delete"))
):
    deleted = await delete_status_svc(db, status_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Import status not found")
    return None
