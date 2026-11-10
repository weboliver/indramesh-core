import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_db
from app.models import Schema
from app.schemas.crud import (
    SchemaCreate, SchemaUpdate, SchemaResponse,
    TemplateColorsUpdate, template_update_to_schema_update,
    schema_to_template_payload, schema_name_to_slug,
    AtomicCreateRequest, AtomicCreateResponse,
    LanguageResponse, SchemaLanguagesUpdate,
)
from app.schemas.auth import UserResponse
from app.services.auth_service import get_current_user, require_permission, require_record_permission
from app.api.pagination import REFERENCE_DEFAULT_LIMIT, MAX_LIMIT
from app.plugin_system.core_data_schemas import SchemaTreeCreate
from app.services.schema_service import (
    create_schema, list_schemas, get_schema, update_schema, delete_schema,
    atomic_create_schema, get_schema_template_data,
    list_schema_languages, set_schema_languages,
    InvalidSchemaLanguage,
    create_schema_from_tree, InvalidSchemaTree, SchemaNameConflict,
    SchemaTreeSourceNotFound,
)
from app.services.classification_service import UnknownClassificationLabel
from app.services.value_lock_service import ValueLockConflict

router = APIRouter(prefix="/schemas", tags=["schemas"])


@router.post("/atomic-create", response_model=AtomicCreateResponse, status_code=status.HTTP_201_CREATED)
async def atomic_create_schema_route(
    request: AtomicCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("schemas", "create"))
):
    try:
        db_schema = await atomic_create_schema(db, request, actor_id=current_user.id)
        return db_schema
    except UnknownClassificationLabel as exc:
        # D-18: fail closed before any write — the template block named a label
        # key this instance does not know, so nothing was created.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Schema creation failed. {str(e)}"
        )


@router.post("/from-tree", response_model=AtomicCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_schema_from_tree_route(
    request: SchemaTreeCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("schemas", "create"))
):
    """Core-owned strict final-save boundary for an accepted schema draft (SEAM-04).

    This is the additive Core create boundary the wizard final-save calls
    directly — it never routes through a plugin, so an already-accepted draft is
    saved while the supplying plugin is disabled (D-08). The whole tree is
    validated before any write: an invalid tree is 422 with the offending
    ``{path, reason}``, a name/key conflict is 409 and a missing Source is 404 —
    nothing is written in any error case (D-09/D-10).
    """
    try:
        return await create_schema_from_tree(db, request, actor_id=current_user.id)
    except SchemaTreeSourceNotFound as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except SchemaNameConflict as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidSchemaTree as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"path": exc.path, "reason": exc.reason},
        )


@router.post("/", response_model=SchemaResponse, status_code=status.HTTP_201_CREATED)
async def create_schema_route(
    schema: SchemaCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("schemas", "create"))
):
    db_schema = await create_schema(db, schema, current_user.id)
    if not db_schema:
        raise HTTPException(status_code=404, detail="Source not found")
    return db_schema


@router.get("/", response_model=List[SchemaResponse])
async def list_schemas_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("schemas", "read")),
    source_id: uuid.UUID | None = Query(None, description="Filter schemas by source ID"),
    name: str | None = Query(None, max_length=255, description="Filter schemas by name (case-insensitive substring match)"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(REFERENCE_DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Maximum records to return"),
    response: Response = None,
):
    items, total = await list_schemas(
        db, source_id=source_id, name=name, skip=skip, limit=limit,
        user_id=current_user.id, is_superuser=current_user.is_superuser,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/{schema_id}", response_model=SchemaResponse)
async def get_schema_route(
    schema_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("schemas", "read", "schema", "schema_id"))
):
    schema = await get_schema(db, schema_id)
    if not schema:
        raise HTTPException(status_code=404, detail="Schema not found")
    return schema


@router.get("/{schema_id}/template-data")
async def get_schema_template_data_route(
    schema_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("schemas", "read", "schema", "schema_id")),
):
    return await get_schema_template_data(db, schema_id)


@router.get("/{schema_id}/template")
async def get_schema_template_route(
    schema_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("schemas", "read", "schema", "schema_id")),
):
    result = await db.execute(select(Schema).where(Schema.id == schema_id))
    schema = result.scalar_one_or_none()
    if not schema:
        raise HTTPException(status_code=404, detail="Schema not found")
    payload = {str(schema.id): schema_to_template_payload(schema)}
    slug = schema_name_to_slug(schema.name)
    filename = f"schema-{slug}-template.json"
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{schema_id}/template", response_model=SchemaResponse)
async def update_schema_template_route(
    schema_id: uuid.UUID,
    template_update: TemplateColorsUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("schemas", "create", "schema", "schema_id")),
):
    fields = template_update_to_schema_update(template_update)
    if not fields:
        result = await db.execute(
            select(Schema)
            .options(selectinload(Schema.default_language), selectinload(Schema.languages))
            .where(Schema.id == schema_id)
        )
        schema = result.scalar_one_or_none()
        if not schema:
            raise HTTPException(status_code=404, detail="Schema not found")
        return schema
    schema_update = SchemaUpdate(**fields)
    try:
        schema = await update_schema(db, schema_id, schema_update, current_user.id)
    except InvalidSchemaLanguage as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not schema:
        raise HTTPException(status_code=404, detail="Schema not found")
    return schema


@router.patch("/{schema_id}", response_model=SchemaResponse)
async def update_schema_route(
    schema_id: uuid.UUID,
    schema_update: SchemaUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("schemas", "update", "schema", "schema_id"))
):
    try:
        schema = await update_schema(db, schema_id, schema_update, current_user.id)
    except InvalidSchemaLanguage as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not schema:
        raise HTTPException(status_code=404, detail="Schema not found")
    return schema


@router.delete("/{schema_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schema_route(
    schema_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("schemas", "delete", "schema", "schema_id"))
):
    try:
        deleted = await delete_schema(db, schema_id, current_user.id)
    except ValueLockConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Schema not found")
    return None


@router.get("/{schema_id}/languages", response_model=List[LanguageResponse])
async def list_schema_languages_route(
    schema_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("schemas", "read", "schema", "schema_id"))
):
    languages = await list_schema_languages(db, schema_id)
    if not languages:
        result = await db.execute(select(Schema).where(Schema.id == schema_id))
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Schema not found")
    return languages


@router.put("/{schema_id}/languages", response_model=List[LanguageResponse])
async def set_schema_languages_route(
    schema_id: uuid.UUID,
    update: SchemaLanguagesUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("schemas", "update", "schema", "schema_id"))
):
    languages = await set_schema_languages(db, schema_id, update.language_codes, current_user.id)
    if languages is None:
        raise HTTPException(status_code=404, detail="Schema not found")
    return languages
