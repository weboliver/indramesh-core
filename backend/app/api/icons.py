import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.crud import (
    IconCreate, IconUpdate, IconResponse
)
from app.schemas.auth import UserResponse
from app.services.auth_service import get_current_user, require_permission
from app.services.icon_service import (
    create_icon, list_icons, get_icon,
    update_icon, delete_icon
)

router = APIRouter(prefix="/icons", tags=["icons"])


@router.post("/", response_model=IconResponse, status_code=status.HTTP_201_CREATED)
async def create_icon_route(
    icon: IconCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("icons", "create"))
):
    return await create_icon(db, icon, current_user.id)


@router.get("/", response_model=List[IconResponse])
async def list_icons_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("icons", "read")),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum records to return"),
    response: Response = None,
):
    items, total = await list_icons(db, skip=skip, limit=limit)
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/{icon_id}", response_model=IconResponse)
async def get_icon_route(
    icon_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("icons", "read"))
):
    icon = await get_icon(db, icon_id)
    if not icon:
        raise HTTPException(status_code=404, detail="Icon not found")
    return icon


@router.patch("/{icon_id}", response_model=IconResponse)
async def update_icon_route(
    icon_id: uuid.UUID,
    icon_update: IconUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("icons", "update"))
):
    icon = await update_icon(db, icon_id, icon_update, current_user.id)
    if not icon:
        raise HTTPException(status_code=404, detail="Icon not found")
    return icon


@router.delete("/{icon_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_icon_route(
    icon_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("icons", "delete"))
):
    deleted = await delete_icon(db, icon_id, current_user.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Icon not found")
    return None
