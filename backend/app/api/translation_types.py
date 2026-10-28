from typing import List
from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.auth import UserResponse
from app.schemas.crud import TranslationTypeResponse
from app.services.auth_service import require_permission
from app.services.translation_service import list_translation_types

router = APIRouter(prefix="/translation-types", tags=["translation-types"])


@router.get("/", response_model=List[TranslationTypeResponse])
async def list_translation_types_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("translations", "read")),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum records to return"),
    response: Response = None,
):
    items, total = await list_translation_types(db, skip=skip, limit=limit)
    response.headers["X-Total-Count"] = str(total)
    return items
