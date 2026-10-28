import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.crud import LanguageCreate, LanguageUpdate, LanguageResponse
from app.schemas.auth import UserResponse
from app.services.auth_service import require_permission
from app.api.pagination import REFERENCE_DEFAULT_LIMIT, MAX_LIMIT
from app.services.language_service import (
    create_language, list_languages, get_language, update_language, delete_language,
)

router = APIRouter(prefix="/languages", tags=["languages"])


@router.post("/", response_model=LanguageResponse, status_code=status.HTTP_201_CREATED)
async def create_language_route(
    language: LanguageCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("languages", "create"))
):
    return await create_language(db, language, current_user.id)


@router.get("/", response_model=List[LanguageResponse])
async def list_languages_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("languages", "read")),
    skip: int = Query(0, ge=0),
    limit: int = Query(REFERENCE_DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    response: Response = None,
):
    items, total = await list_languages(db, skip=skip, limit=limit)
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/{language_id}", response_model=LanguageResponse)
async def get_language_route(
    language_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("languages", "read"))
):
    language = await get_language(db, language_id)
    if not language:
        raise HTTPException(status_code=404, detail="Language not found")
    return language


@router.patch("/{language_id}", response_model=LanguageResponse)
async def update_language_route(
    language_id: uuid.UUID,
    language_update: LanguageUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("languages", "update"))
):
    language = await update_language(db, language_id, language_update, current_user.id)
    if not language:
        raise HTTPException(status_code=404, detail="Language not found")
    return language


@router.delete("/{language_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_language_route(
    language_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("languages", "delete"))
):
    deleted = await delete_language(db, language_id, current_user.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Language not found")
    return None
