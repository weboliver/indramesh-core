"""The per-instance classification label catalogue over REST (D-12, D-14).

Two routers share one module because both paths belong to one resource family:
``classification-families`` is read-only by design (D-02 — families come as data,
never as an operator CRUD surface) and ``classification-labels`` is the admin
maintenance surface (D-14/D-15). There is deliberately no endpoint that creates
or deletes a family.
"""

import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.auth import UserResponse
from app.schemas.classification import (
    ClassificationFamilyResponse,
    ClassificationLabelCreate,
    ClassificationLabelResponse,
    ClassificationLabelUpdate,
)
from app.services.auth_service import require_permission
from app.services.classification_service import (
    ClassificationConflict,
    ClassificationNotFound,
    create_label,
    delete_label,
    list_families,
    update_label,
)
from app.services.translation_service import validate_read_language

families_router = APIRouter(
    prefix="/classification-families", tags=["classification-families"]
)
labels_router = APIRouter(
    prefix="/classification-labels", tags=["classification-labels"]
)


@families_router.get("/", response_model=List[ClassificationFamilyResponse])
async def list_classification_families(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(
        require_permission("object-type-attributes", "read")
    ),
    lang: str | None = Query(
        None,
        description=(
            "Resolve label names in the given language "
            "(falls back to the base name when no translation exists)"
        ),
    ),
    response: Response = None,
):
    """Every family with its labels, both in display order (D-12, D-13)."""
    if lang:
        # Unknown/inactive language -> 400 before any row is read.
        await validate_read_language(db, lang, None)
    families = await list_families(db, lang)
    response.headers["X-Total-Count"] = str(len(families))
    return families


def _conflict(error: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))


@labels_router.post(
    "/",
    response_model=ClassificationLabelResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_classification_label(
    payload: ClassificationLabelCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("settings", "update")),
):
    """Add an operator label to `confidentiality` or `export_control` (D-14)."""
    try:
        label = await create_label(db, payload, current_user.id)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    except ClassificationConflict as error:
        raise _conflict(error) from error
    return label


@labels_router.patch("/{label_id}", response_model=ClassificationLabelResponse)
async def update_classification_label(
    label_id: uuid.UUID,
    payload: ClassificationLabelUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("settings", "update")),
):
    """Rename and/or reorder one label; `key` and `family` are immutable (D-14)."""
    try:
        label = await update_label(db, label_id, payload, current_user.id)
    except ClassificationNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Classification label not found",
        ) from error
    except ClassificationConflict as error:
        raise _conflict(error) from error
    return label


@labels_router.delete(
    "/{label_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_classification_label(
    label_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("settings", "update")),
):
    """Delete one unassigned operator label and its translations (D-15)."""
    try:
        await delete_label(db, label_id, current_user.id)
    except ClassificationNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Classification label not found",
        ) from error
    except ClassificationConflict as error:
        raise _conflict(error) from error
