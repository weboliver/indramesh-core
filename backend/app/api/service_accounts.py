import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.crud import (
    ServiceAccountCreate, ServiceAccountUpdate, ServiceAccountResponse
)
from app.schemas.auth import UserResponse
from app.services.auth_service import get_current_user, require_permission
from app.services.service_account_service import (
    create_service_account, list_service_accounts, get_service_account as get_account_svc,
    update_service_account as update_account_svc, delete_service_account as delete_account_svc
)

router = APIRouter(prefix="/service-accounts", tags=["service-accounts"])


@router.post("/", response_model=ServiceAccountResponse, status_code=status.HTTP_201_CREATED)
async def create_service_account_route(
    account_data: ServiceAccountCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("service-accounts", "create"))
):
    try:
        return await create_service_account(db, account_data, current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/", response_model=List[ServiceAccountResponse])
async def list_service_accounts_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("service-accounts", "read")),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum records to return"),
    source_id: uuid.UUID | None = Query(None, description="Filter by source ID"),
    target_id: uuid.UUID | None = Query(None, description="Filter by target ID"),
    response: Response = None,
):
    items, total = await list_service_accounts(
        db, skip=skip, limit=limit, source_id=source_id, target_id=target_id
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/{account_id}", response_model=ServiceAccountResponse)
async def get_service_account_route(
    account_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("service-accounts", "read"))
):
    account = await get_account_svc(db, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Service account not found")
    return account


@router.patch("/{account_id}", response_model=ServiceAccountResponse)
async def update_service_account_route(
    account_id: uuid.UUID,
    account_update: ServiceAccountUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("service-accounts", "update"))
):
    account = await update_account_svc(db, account_id, account_update, current_user.id)
    if not account:
        raise HTTPException(status_code=404, detail="Service account not found")
    return account


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_service_account_route(
    account_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("service-accounts", "delete"))
):
    deleted = await delete_account_svc(db, account_id, current_user.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Service account not found")
    return None
