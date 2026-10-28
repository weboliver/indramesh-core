import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.crud import (
    StatusCreate, StatusUpdate, StatusResponse
)
from app.schemas.auth import UserResponse
from app.services.auth_service import get_current_user, require_permission
from app.api.pagination import REFERENCE_DEFAULT_LIMIT, MAX_LIMIT
from app.services.status_service import (
    create_status, list_statuses, get_status,
    update_status, delete_status
)

router = APIRouter(prefix="/statuses", tags=["statuses"])


@router.post("/", response_model=StatusResponse, status_code=status.HTTP_201_CREATED)
async def create_status_route(
    status_data: StatusCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("statuses", "create"))
):
    try:
        return await create_status(db, status_data, current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/", response_model=List[StatusResponse])
async def list_statuses_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("statuses", "read")),
    schema_id: uuid.UUID = Query(None, description="Filter by schema"),
    lang: str | None = Query(None, description="Resolve translated name for this language code (BCP-47, e.g. 'de')"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(REFERENCE_DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Maximum records to return"),
    response: Response = None,
):
    items, total = await list_statuses(db, skip=skip, limit=limit, schema_id=schema_id, lang=lang)
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/{status_id}", response_model=StatusResponse)
async def get_status_route(
    status_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("statuses", "read")),
    lang: str | None = Query(None, description="Resolve translated name for this language code (BCP-47, e.g. 'de')"),
):
    status_obj = await get_status(db, status_id, lang=lang)
    if not status_obj:
        raise HTTPException(status_code=404, detail="Status not found")
    return status_obj


@router.patch("/{status_id}", response_model=StatusResponse)
async def update_status_route(
    status_id: uuid.UUID,
    status_update: StatusUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("statuses", "update"))
):
    try:
        status_obj = await update_status(db, status_id, status_update, current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if not status_obj:
        raise HTTPException(status_code=404, detail="Status not found")
    return status_obj


@router.delete("/{status_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_status_route(
    status_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("statuses", "delete"))
):
    deleted = await delete_status(db, status_id, current_user.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Status not found")
    return None
