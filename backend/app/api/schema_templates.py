import uuid
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from pydantic import BaseModel
from sqlalchemy import select, func, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import SchemaTemplate
from app.schemas.crud import (
    SchemaTemplateCreate, SchemaTemplateUpdate, SchemaTemplateResponse,
    AtomicCreateRequest, SchemaResponse,
)
from app.schemas.auth import UserResponse
from app.services.auth_service import get_current_user, require_permission
from app.services.schema_service import atomic_create_schema
from app.services.classification_service import UnknownClassificationLabel
from app.services.audit_service import audit_log, model_to_dict


class InstantiateTemplateRequest(BaseModel):
    source_id: uuid.UUID
    name: Optional[str] = None


router = APIRouter(prefix="/schema-templates", tags=["schema-templates"])


@router.post("/", response_model=SchemaTemplateResponse, status_code=status.HTTP_201_CREATED)
async def create_schema_template(
    template: SchemaTemplateCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("schema-templates", "create"))
):
    db_template = SchemaTemplate(**template.model_dump())
    db.add(db_template)
    await db.flush()
    new_values = model_to_dict(db_template)
    await audit_log(db, "schema_templates", db_template.id, "CREATE", current_user.id,
                    old_values=None, new_values=new_values)
    await db.commit()
    await db.refresh(db_template)
    return db_template


@router.get("/", response_model=List[SchemaTemplateResponse])
async def list_schema_templates(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("schema-templates", "read")),
    category: str = Query(None, description="Filter by category"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(50, ge=1, le=1000, description="Maximum records to return"),
    response: Response = None,
):
    base_query = select(SchemaTemplate)
    count_query = select(func.count()).select_from(SchemaTemplate)

    if category:
        base_query = base_query.where(SchemaTemplate.category == category)
        count_query = count_query.where(SchemaTemplate.category == category)

    total_result = await db.execute(count_query)
    total = total_result.scalar()

    result = await db.execute(
        base_query.offset(skip).limit(limit).order_by(SchemaTemplate.name)
    )
    items = result.scalars().all()

    response.headers["X-Total-Count"] = str(total)
    return items


@router.post("/{template_id}/instantiate", response_model=SchemaResponse, status_code=status.HTTP_201_CREATED)
async def instantiate_schema_template(
    template_id: uuid.UUID,
    request: InstantiateTemplateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("schema-templates", "create")),
):
    result = await db.execute(
        select(SchemaTemplate).where(SchemaTemplate.id == template_id)
    )
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Schema template not found")

    template_data = template.template_data or {}
    schema_name = request.name or template_data.get("schema_name", "Untitled Schema")
    object_types = template_data.get("object_types", [])

    # AtomicCreateAttribute.select_options is typed List[str], but
    # get_schema_template_data() used to (before 2026-07-27) store Jira-synced
    # Select attributes' options as List[{"value": ..., "position": ...}]
    # straight from ObjectTypeAttribute.structure — any template saved before
    # that fix still has that shape frozen in template_data, and constructing
    # AtomicCreateRequest below raised an unhandled Pydantic ValidationError
    # (500, "Internal server error") for every such template. Normalize here
    # defensively so already-stored templates keep working too, not just ones
    # created going forward.
    for ot in object_types:
        for attr in ot.get("attributes", []):
            raw_options = attr.get("select_options")
            if raw_options and not all(isinstance(o, str) for o in raw_options):
                attr["select_options"] = [
                    (o if isinstance(o, str) else o.get("value") or o.get("label"))
                    for o in raw_options
                ]
                attr["select_options"] = [o for o in attr["select_options"] if o]

    atomic_request = AtomicCreateRequest(
        source_id=request.source_id,
        name=schema_name,
        description=template_data.get("schema_description"),
        object_types=object_types,
        statuses=template_data.get("statuses", []),
    )

    try:
        db_schema = await atomic_create_schema(db, atomic_request, actor_id=current_user.id)
    except UnknownClassificationLabel as exc:
        # D-18: a template naming a label key this instance does not know is a
        # client error about the payload — never a 500, and nothing is created
        # because every key was resolved before the first write.
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Schema instantiation failed. {str(e)}")

    await db.execute(
        sql_update(SchemaTemplate)
        .where(SchemaTemplate.id == template_id)
        .values(usage_count=SchemaTemplate.usage_count + 1)
    )
    await db.commit()

    return db_schema


@router.get("/{template_id}", response_model=SchemaTemplateResponse)
async def get_schema_template(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("schema-templates", "read"))
):
    result = await db.execute(
        select(SchemaTemplate).where(SchemaTemplate.id == template_id)
    )
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Schema template not found")
    return template


@router.patch("/{template_id}", response_model=SchemaTemplateResponse)
async def update_schema_template(
    template_id: uuid.UUID,
    template_update: SchemaTemplateUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("schema-templates", "update"))
):
    result = await db.execute(
        select(SchemaTemplate).where(SchemaTemplate.id == template_id)
    )
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Schema template not found")

    old_values = model_to_dict(template)
    update_data = template_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(template, field, value)

    new_values = model_to_dict(template)
    await audit_log(db, "schema_templates", template_id, "UPDATE", current_user.id,
                    old_values=old_values, new_values=new_values)
    await db.commit()
    await db.refresh(template)
    return template


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schema_template(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("schema-templates", "delete"))
):
    result = await db.execute(
        select(SchemaTemplate).where(SchemaTemplate.id == template_id)
    )
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Schema template not found")
    old_values = model_to_dict(template)
    await audit_log(db, "schema_templates", template_id, "DELETE", current_user.id,
                    old_values=old_values, new_values=None)
    await db.delete(template)
    await db.commit()
    return None
