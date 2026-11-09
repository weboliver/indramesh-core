import logging
import uuid
from typing import List

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.auth import UserResponse
from app.schemas.crud import (
    TranslationUpsert,
    TranslationResponse,
)
from app.services.auth_service import require_permission
from app.services.translation_service import (
    upsert_translation,
    list_translations,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/translations", tags=["translations"])


@router.put("/", response_model=TranslationResponse)
async def upsert_translation_route(
    data: TranslationUpsert,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("translations", "update")),
):
    return await upsert_translation(db, data, current_user.id)


@router.get("/", response_model=List[TranslationResponse])
async def list_translations_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("translations", "read")),
    translation_type: str | None = Query(None, description="Filter by seeded translation type code"),
    entity_id: uuid.UUID | None = Query(None, description="Filter by entity UUID"),
    field: str | None = Query(None, description="Filter by field name (name/description)"),
    language_code: str | None = Query(None, description="Filter by language code"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum records to return"),
    response: Response = None,
):
    items, total = await list_translations(
        db, translation_type=translation_type, entity_id=entity_id,
        field=field, language_code=language_code, skip=skip, limit=limit,
        user_id=str(current_user.id), is_superuser=current_user.is_superuser,
    )
    response.headers["X-Total-Count"] = str(total)
    return items
