import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import ObjectTypeAttribute
from app.schemas.crud import (
    ObjectTypeAttributeCreate, ObjectTypeAttributeUpdate, ObjectTypeAttributeResponse
)
from app.schemas.auth import UserResponse
from app.services.auth_service import require_permission
from app.services.object_type_attribute_service import (
    create_object_type_attribute, list_object_type_attributes,
    get_object_type_attribute, update_object_type_attribute,
    delete_object_type_attribute
)
from app.services.value_visibility import assert_container_access

router = APIRouter(prefix="/object-type-attributes", tags=["object-type-attributes"])

_MISSING_ATTRIBUTE = "Object type attribute not found"


async def _load_attribute_or_404(db: AsyncSession, attribute_id: uuid.UUID):
    """Direct ORM read of the attribute row (never the cached service getter).

    ``get_object_type_attribute`` returns the serialized response dict on a
    cache hit, so reading ``object_type_id`` off it would answer 500 once the
    single-entity cache is warm. The gate needs the stored row.
    """
    existing = await db.get(ObjectTypeAttribute, attribute_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=_MISSING_ATTRIBUTE)
    return existing



@router.post("/", response_model=ObjectTypeAttributeResponse, status_code=status.HTTP_201_CREATED)
async def create_object_type_attribute_route(
    attribute: ObjectTypeAttributeCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("object-type-attributes", "create"))
):
    # AUTHZ-07 (D-01/D-03): the object type is the container of a new attribute.
    # Readability first (404, the route's existing missing-container answer),
    # then create (403), both before the service call.
    if not current_user.is_superuser:
        await assert_container_access(
            str(current_user.id),
            current_user.is_superuser,
            entity_type="object_type",
            container_id=attribute.object_type_id,
            action="create",
            not_found_detail="Referenced entity not found",
        )
        # IN-01 / D-17 (319 D-03): the referenced ObjectType is a second
        # container, and it only has to be READABLE — the attribute points at
        # it, nothing is written there. The route's missing-container detail is
        # reused verbatim, so an invisible target is indistinguishable from a
        # nonexistent one.
        if attribute.referenced_object_type_id is not None:
            await assert_container_access(
                str(current_user.id),
                current_user.is_superuser,
                entity_type="object_type",
                container_id=attribute.referenced_object_type_id,
                action="read",
                not_found_detail="Referenced entity not found",
            )
    try:
        db_attribute = await create_object_type_attribute(db, attribute, actor_id=current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not db_attribute:
        raise HTTPException(status_code=404, detail="Referenced entity not found")
    return db_attribute


@router.get("/", response_model=List[ObjectTypeAttributeResponse])
async def list_object_type_attributes_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("object-type-attributes", "read")),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum records to return"),
    object_type_id: uuid.UUID = Query(None, description="Filter by object type ID"),
    lang: str | None = Query(None, description="Resolve name/description in the given language (falls back to schema default language)"),
    response: Response = None,
):
    items, total = await list_object_type_attributes(
        db, skip=skip, limit=limit, object_type_id=object_type_id,
        user_id=current_user.id, is_superuser=current_user.is_superuser,
        lang=lang,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/{attribute_id}", response_model=ObjectTypeAttributeResponse)
async def get_object_type_attribute_route(
    attribute_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("object-type-attributes", "read")),
    lang: str | None = Query(None, description="Resolve name/description in the given language (falls back to schema default language)"),
):
    attribute = await get_object_type_attribute(db, attribute_id, lang)
    if not attribute:
        raise HTTPException(status_code=404, detail="Object type attribute not found")
    return attribute


@router.patch("/{attribute_id}", response_model=ObjectTypeAttributeResponse)
async def update_object_type_attribute_route(
    attribute_id: uuid.UUID,
    attribute_update: ObjectTypeAttributeUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("object-type-attributes", "update"))
):
    # AUTHZ-07 (D-01/D-03): the attribute's current container is gated first,
    # then a container supplied in the body ("move into a denied container").
    # Readability answers 404, the update action 403, both before the service
    # call so a refusal leaves the row untouched.
    if not current_user.is_superuser:
        existing = await _load_attribute_or_404(db, attribute_id)
        await assert_container_access(
            str(current_user.id),
            current_user.is_superuser,
            entity_type="object_type",
            container_id=existing.object_type_id,
            action="update",
            not_found_detail=_MISSING_ATTRIBUTE,
        )
        target_id = attribute_update.object_type_id
        if target_id is not None and target_id != existing.object_type_id:
            await assert_container_access(
                str(current_user.id),
                current_user.is_superuser,
                entity_type="object_type",
                container_id=target_id,
                action="update",
                not_found_detail=_MISSING_ATTRIBUTE,
            )
        # IN-01 / D-17 (319 D-03): a supplied reference target is a second
        # container and the update-side sibling of the create gate below — it
        # only has to be READABLE, because the attribute points at it and
        # nothing is written there.
        if attribute_update.referenced_object_type_id is not None:
            await assert_container_access(
                str(current_user.id),
                current_user.is_superuser,
                entity_type="object_type",
                container_id=attribute_update.referenced_object_type_id,
                action="read",
                not_found_detail=_MISSING_ATTRIBUTE,
            )
    try:
        attribute = await update_object_type_attribute(db, attribute_id, attribute_update, actor_id=current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not attribute:
        raise HTTPException(status_code=404, detail="Object type attribute not found")
    return attribute


@router.delete("/{attribute_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_object_type_attribute_route(
    attribute_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("object-type-attributes", "delete"))
):
    # AUTHZ-07 (D-01/D-03): `delete` is a separate stored action — a `write`
    # DENY must not block a delete and a `delete` DENY must. Readability answers
    # 404, the delete action 403, both before the service call.
    if not current_user.is_superuser:
        existing = await _load_attribute_or_404(db, attribute_id)
        await assert_container_access(
            str(current_user.id),
            current_user.is_superuser,
            entity_type="object_type",
            container_id=existing.object_type_id,
            action="delete",
            not_found_detail=_MISSING_ATTRIBUTE,
        )
    try:
        deleted = await delete_object_type_attribute(db, attribute_id, actor_id=current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Object type attribute not found")
    return None
