import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.api_client import (
    ApiClientCreate, ApiClientUpdate, ApiClientResponse, ApiClientCreateResponse
)
from app.schemas.auth import UserResponse
from app.services.auth_service import require_permission
from app.services import api_client_service

router = APIRouter(prefix="/api-clients", tags=["api-clients"])


@router.post("/", response_model=ApiClientCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_api_client_route(
    client_data: ApiClientCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("api-clients", "create")),
):
    client, raw_token = await api_client_service.create_api_client(db, client_data, current_user.id)
    return ApiClientCreateResponse(
        **ApiClientResponse.model_validate(client).model_dump(), token=raw_token
    )


@router.get("/", response_model=List[ApiClientResponse])
async def list_api_clients_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("api-clients", "read")),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    response: Response = None,
):
    items, total = await api_client_service.list_api_clients(db, skip=skip, limit=limit)
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/{client_id}", response_model=ApiClientResponse)
async def get_api_client_route(
    client_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("api-clients", "read")),
):
    client = await api_client_service.get_api_client(db, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="API client not found")
    return client


@router.patch("/{client_id}", response_model=ApiClientResponse)
async def update_api_client_route(
    client_id: uuid.UUID,
    client_update: ApiClientUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("api-clients", "update")),
):
    client = await api_client_service.update_api_client(db, client_id, client_update, current_user.id)
    if not client:
        raise HTTPException(status_code=404, detail="API client not found")
    return client


@router.post("/{client_id}/revoke", response_model=ApiClientResponse)
async def revoke_api_client_route(
    client_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("api-clients", "revoke")),
):
    client = await api_client_service.revoke_api_client(db, client_id, current_user.id)
    if not client:
        raise HTTPException(status_code=404, detail="API client not found")
    return client


@router.delete("/{client_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_api_client_route(
    client_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("api-clients", "delete")),
):
    try:
        client = await api_client_service.delete_api_client(db, client_id, current_user.id)
    except api_client_service.ApiClientNotRevokedError:
        raise HTTPException(
            status_code=400,
            detail="API client must be revoked before it can be deleted",
        )
    if not client:
        raise HTTPException(status_code=404, detail="API client not found")
