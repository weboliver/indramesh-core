import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.crud import (
    TargetCreate, TargetUpdate, TargetResponse
)
from app.schemas.auth import UserResponse
from app.services.auth_service import get_current_user, require_permission
from app.services.target_service import (
    create_target, list_targets, get_target as get_target_svc,
    update_target as update_target_svc, delete_target as delete_target_svc
)

router = APIRouter(prefix="/targets", tags=["targets"])


@router.post("/", response_model=TargetResponse, status_code=status.HTTP_201_CREATED)
async def create_target_route(
    target_data: TargetCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("targets", "create"))
):
    try:
        return await create_target(db, target_data, current_user.id)
    except ValueError as e:
        detail = str(e)
        if "Schema not found" in detail:
            raise HTTPException(status_code=404, detail=detail)
        # Config validation errors from model_validator
        raise HTTPException(status_code=422, detail=detail)


@router.get("/", response_model=List[TargetResponse])
async def list_targets_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("targets", "read")),
    name: str | None = Query(None, max_length=255, description="Filter targets by name (case-insensitive substring match)"),
    connector_type: str | None = Query(None, max_length=50, description="Filter targets by connector type (normalized lowercase string)"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum records to return"),
    response: Response = None,
):
    items, total = await list_targets(db, name=name, connector_type=connector_type, skip=skip, limit=limit)
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/{target_id}", response_model=TargetResponse)
async def get_target_route(
    target_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("targets", "read"))
):
    target = await get_target_svc(db, target_id)
    if not target:
        raise HTTPException(status_code=404, detail="Target not found")
    return target


@router.patch("/{target_id}", response_model=TargetResponse)
async def update_target_route(
    target_id: uuid.UUID,
    target_update: TargetUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("targets", "update"))
):
    try:
        target = await update_target_svc(db, target_id, target_update, current_user.id)
        if not target:
            raise HTTPException(status_code=404, detail="Target not found")
        return target
    except ValueError as e:
        detail = str(e)
        if "Schema not found" in detail:
            raise HTTPException(status_code=404, detail=detail)
        # Config validation errors from model_validator
        raise HTTPException(status_code=422, detail=detail)


@router.delete("/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_target_route(
    target_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("targets", "delete"))
):
    deleted = await delete_target_svc(db, target_id, current_user.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Target not found")
    return None
