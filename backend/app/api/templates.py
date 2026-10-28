import uuid
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import Schema
from app.schemas.auth import UserResponse
from app.schemas.crud import (
    SchemaUpdate,
    TemplateColorsUpdate,
    TemplateBulkImportResponse,
    schema_to_template_payload,
    template_update_to_schema_update,
)
from app.services.auth_service import require_permission
from app.services.schema_service import update_schema


router = APIRouter(prefix="/templates", tags=["templates"])


@router.get("/export")
async def export_all_templates_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("schemas", "read")),
):
    result = await db.execute(select(Schema))
    schemas = result.scalars().all()
    payload = {str(s.id): schema_to_template_payload(s) for s in schemas}
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": 'attachment; filename="schemas-templates-export.json"'},
    )


@router.post("/import", response_model=TemplateBulkImportResponse)
async def bulk_import_templates_route(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("schemas", "update")),
):
    updated: list[str] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    for schema_id_str, raw_colors in payload.items():
        try:
            schema_uuid = uuid.UUID(schema_id_str)
        except (ValueError, TypeError):
            skipped.append({"id": schema_id_str, "reason": "Invalid UUID format"})
            continue
        try:
            template_update = TemplateColorsUpdate.model_validate(raw_colors or {})
        except Exception as e:
            errors.append({"id": schema_id_str, "reason": f"Invalid color payload: {e}"})
            continue
        fields = template_update_to_schema_update(template_update)
        if not fields:
            updated.append(schema_id_str)
            continue
        try:
            schema_update = SchemaUpdate(**fields)
            updated_schema = await update_schema(db, schema_uuid, schema_update, current_user.id)
            if updated_schema is None:
                skipped.append({"id": schema_id_str, "reason": "Schema not found"})
            else:
                updated.append(schema_id_str)
        except Exception as e:
            errors.append({"id": schema_id_str, "reason": str(e)})
    return TemplateBulkImportResponse(updated=updated, skipped=skipped, errors=errors)
