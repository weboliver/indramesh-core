import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import ObjectType, Target, ExportDefinition, Schedule
from app.schemas.crud import (
    ExportDefinitionCreate, ExportDefinitionUpdate, ExportDefinitionResponse
)
from app.schemas.auth import UserResponse
from app.services.auth_service import require_permission
from app.services.audit_service import audit_log, model_to_dict

router = APIRouter(prefix="/export-definitions", tags=["export-definitions"])


@router.post("/", response_model=ExportDefinitionResponse, status_code=status.HTTP_201_CREATED)
async def create_export_definition(
    definition: ExportDefinitionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("export-definitions", "create"))
):
    result = await db.execute(
        select(ObjectType).where(ObjectType.id == definition.object_type_id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="ObjectType not found")

    if definition.target_id is not None:
        result = await db.execute(
            select(Target).where(Target.id == definition.target_id)
        )
        target = result.scalar_one_or_none()
        if not target:
            raise HTTPException(status_code=404, detail="Target not found")
        if target.connector_type != "csv":
            raise HTTPException(
                status_code=422,
                detail=f"Target connector_type must be 'csv', got '{target.connector_type}'",
            )

    # mode="json" — FieldMappingItem.attr_id (uuid.UUID) and any datetime fields must be
    # serialized to JSON-safe strings before hitting the plain JSON column; model_dump()
    # default (mode="python") leaves them as raw objects and json.dumps raises TypeError
    # at INSERT time.
    db_def = ExportDefinition(**definition.model_dump(mode="json"))
    db.add(db_def)
    await db.flush()
    new_values = model_to_dict(db_def)
    await audit_log(db, "export_definitions", db_def.id, "CREATE", current_user.id,
                    old_values=None, new_values=new_values)
    await db.commit()
    await db.refresh(db_def)
    return db_def


@router.get("/", response_model=List[ExportDefinitionResponse])
async def list_export_definitions(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("export-definitions", "read")),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum records to return"),
    response: Response = None,
):
    count_result = await db.execute(
        select(func.count()).select_from(ExportDefinition)
    )
    total = count_result.scalar()

    result = await db.execute(
        select(ExportDefinition).offset(skip).limit(limit).order_by(ExportDefinition.name)
    )
    items = result.scalars().all()

    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/{definition_id}", response_model=ExportDefinitionResponse)
async def get_export_definition(
    definition_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("export-definitions", "read"))
):
    result = await db.execute(
        select(ExportDefinition).where(ExportDefinition.id == definition_id)
    )
    definition = result.scalar_one_or_none()
    if not definition:
        raise HTTPException(status_code=404, detail="Export definition not found")
    return definition


@router.patch("/{definition_id}", response_model=ExportDefinitionResponse)
async def update_export_definition(
    definition_id: uuid.UUID,
    definition_update: ExportDefinitionUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("export-definitions", "update"))
):
    result = await db.execute(
        select(ExportDefinition).where(ExportDefinition.id == definition_id)
    )
    definition = result.scalar_one_or_none()
    if not definition:
        raise HTTPException(status_code=404, detail="Export definition not found")

    old_values = model_to_dict(definition)
    if definition_update.object_type_id is not None:
        result = await db.execute(
            select(ObjectType).where(ObjectType.id == definition_update.object_type_id)
        )
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="ObjectType not found")

    if definition_update.target_id is not None:
        result = await db.execute(
            select(Target).where(Target.id == definition_update.target_id)
        )
        target = result.scalar_one_or_none()
        if not target:
            raise HTTPException(status_code=404, detail="Target not found")
        if target.connector_type != "csv":
            raise HTTPException(
                status_code=422,
                detail=f"Target connector_type must be 'csv', got '{target.connector_type}'",
            )

    # See create_export_definition — mode="json" ensures UUID/datetime fields are
    # JSON-serializable before touching the JSON column.
    update_data = definition_update.model_dump(mode="json", exclude_unset=True)
    for field, value in update_data.items():
        setattr(definition, field, value)

    new_values = model_to_dict(definition)
    await audit_log(db, "export_definitions", definition_id, "UPDATE", current_user.id,
                    old_values=old_values, new_values=new_values)
    await db.commit()
    await db.refresh(definition)
    return definition


@router.delete("/{definition_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_export_definition(
    definition_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("export-definitions", "delete"))
):
    result = await db.execute(
        select(ExportDefinition).where(ExportDefinition.id == definition_id)
    )
    definition = result.scalar_one_or_none()
    if not definition:
        raise HTTPException(status_code=404, detail="Export definition not found")

    if definition.is_permanent:
        schedule_result = await db.execute(
            select(Schedule.id, Schedule.name).where(
                Schedule.template_type == "export",
                Schedule.template_id == definition_id,
            )
        )
        schedules = schedule_result.all()
        if schedules:
            names = ", ".join(f"'{s.name}' (id={s.id})" for s in schedules)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot delete ExportDefinition '{definition.name}': it is marked as permanent "
                       f"and is referenced by schedule(s): {names}. Unlink the schedule(s) first.",
            )

    old_values = model_to_dict(definition)
    await audit_log(db, "export_definitions", definition_id, "DELETE", current_user.id,
                    old_values=old_values, new_values=None)
    await db.delete(definition)
    await db.commit()
    return None
