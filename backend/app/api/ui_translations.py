"""GET /ui-translations/dictionary, GET /ui-translations/keys, GET
/ui-translations/catalog, POST /ui-translations/export and the per-key
systemui delete route.

Phase 230 / STOR-03 — read-only dictionary/keys/catalog routes over the
schema-less systemui store, reusing the existing "translations:read"/
"translations:update" permissions (no new permission resource). Per-key
systemui upserts still go through the existing natural-key
PUT /api/v1/translations/ endpoint (ARCHITECTURE D1) — that has NOT changed.

Phase 307 / AITR-03 removed the Phase 234 bulk-translation lifecycle from
Core: preview/enqueue/status/review/apply/dismiss now live in the
``ai-translation`` plugin under ``/api/v1/plugins/ai-translation``. This
router owns only the manual systemui surfaces; active ``Translation`` row
writes still go through the existing audited ``upsert_translation()`` path.
"""
import logging
import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.db.redis import get_redis
from app.schemas.auth import UserResponse
from app.schemas.crud import (
    UiDictionaryResponse,
    UiStringResponse,
    UiTranslationCatalogResponse,
    UiTranslationExportResponse,
)
from app.services.auth_service import require_permission
from app.services.translation_service import delete_systemui_translation
from app.services.ui_translation_export_service import export_ui_translation_dictionaries
from app.services.ui_translation_service import get_ui_translation_catalog, list_ui_keys, resolve_ui_dictionary

# Phase 235 / D-12: single fixed lock key, short bounded TTL — the export's
# critical section (resolve + stage + publish a handful of small JSON files)
# completes in well under a second, so this is generous crash-recovery
# headroom, not a tight bound requiring renewal (235-PLAN-REVIEW.md Finding 2).
UI_TRANSLATION_EXPORT_LOCK_KEY = "ui_translation_export_lock"
UI_TRANSLATION_EXPORT_LOCK_TTL = 60

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ui-translations", tags=["ui-translations"])


@router.get("/dictionary", response_model=UiDictionaryResponse)
async def get_ui_dictionary_route(
    lang: str = Query(..., min_length=2, max_length=10),
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("translations", "read")),
):
    return await resolve_ui_dictionary(db, lang)


@router.get("/keys", response_model=List[UiStringResponse])
async def list_ui_keys_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("translations", "read")),
    namespace: str | None = Query(None, description="Filter by namespace"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum records to return"),
    response: Response = None,
):
    items, total = await list_ui_keys(db, skip=skip, limit=limit, namespace=namespace)
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/catalog", response_model=UiTranslationCatalogResponse)
async def get_ui_translation_catalog_route(
    editor_language: str = Query(..., min_length=2, max_length=10),
    namespace: str | None = Query(None, description="Filter by exact namespace"),
    namespace_is_null: bool = Query(False, description="Select only UiString.namespace IS NULL rows"),
    q: str | None = Query(None, max_length=255, description="Case-insensitive substring search on key/effective value"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=200, description="Maximum records to return"),
    ui_string_id: uuid.UUID | None = Query(None, description="Restrict to a single registry key (used for post-mutation row refresh)"),
    only_missing: bool = Query(False, description="Restrict to keys with no explicit non-blank selected-language value"),
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("translations", "read")),
):
    catalog = await get_ui_translation_catalog(
        db,
        editor_language=editor_language,
        namespace=namespace,
        namespace_is_null=namespace_is_null,
        q=q,
        skip=skip,
        limit=limit,
        ui_string_id=ui_string_id,
        only_missing=only_missing,
    )
    # Phase 235 / D-09: append the backend-derived availability flag here,
    # before FastAPI validates against UiTranslationCatalogResponse — the
    # service function stays free of environment concerns.
    catalog["export_available"] = settings.environment == "development"
    return catalog


@router.post("/export", response_model=UiTranslationExportResponse)
async def export_ui_translations_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("translations", "update")),
):
    """Phase 235 / EXP-01: development-only, dev-admin-triggered filesystem
    snapshot of every active system UI language's resolved dictionary.

    D-10: the environment gate is enforced here independently of the
    catalog's advisory flag — a forged request against a non-development
    backend gets 403 before any lock acquisition or service/filesystem work.
    D-12: a single SET NX EX Redis lock serializes the endpoint; a held lock
    returns 409 with no second export invocation, and this request's own
    marker is released in `finally` on every outcome.
    """
    if settings.environment != "development":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Filesystem export is only available in the development environment",
        )

    redis = await get_redis()
    acquired = await redis.set(
        UI_TRANSLATION_EXPORT_LOCK_KEY, "running", ex=UI_TRANSLATION_EXPORT_LOCK_TTL, nx=True
    )
    if not acquired:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A translation export is already running",
        )

    try:
        paths = await export_ui_translation_dictionaries(db, current_user.id)
    finally:
        await redis.delete(UI_TRANSLATION_EXPORT_LOCK_KEY)

    return UiTranslationExportResponse(paths=paths)


@router.delete("/keys/{ui_string_id}/translations/{language_code}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ui_translation_route(
    ui_string_id: uuid.UUID,
    language_code: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("translations", "delete")),
):
    await delete_systemui_translation(db, ui_string_id, language_code, current_user.id)
    return None
