import uuid
from typing import List

from fastapi import HTTPException, status
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Language, Schema, SchemaLanguage
from app.schemas.crud import LanguageCreate, LanguageUpdate
from app.services.audit_service import audit_log, model_to_dict


async def create_language(db, language_data: LanguageCreate, actor_id: uuid.UUID) -> Language:
    db_language = Language(code=language_data.code, name=language_data.name, is_active=language_data.is_active)
    db.add(db_language)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Language code '{language_data.code}' already exists.")
    await audit_log(db, "languages", db_language.id, "CREATE", actor_id,
                    old_values=None, new_values=model_to_dict(db_language))
    await db.commit()
    await db.refresh(db_language)
    return db_language


async def list_languages(db, skip: int = 0, limit: int = 100) -> tuple[List[Language], int]:
    count_query = select(func.count()).select_from(Language)
    query = select(Language).order_by(Language.code).offset(skip).limit(limit)
    total = (await db.execute(count_query)).scalar()
    items = (await db.execute(query)).scalars().all()
    return list(items), total


async def get_language(db, language_id: uuid.UUID) -> Language | None:
    result = await db.execute(select(Language).where(Language.id == language_id))
    return result.scalar_one_or_none()


async def update_language(db, language_id: uuid.UUID, language_update: LanguageUpdate, actor_id: uuid.UUID) -> Language | None:
    result = await db.execute(select(Language).where(Language.id == language_id))
    language = result.scalar_one_or_none()
    if not language:
        return None
    old_values = model_to_dict(language)
    # D-03: deactivation is blocked ONLY when the language is a schema's default.
    # Subset membership does NOT block; the subset entry stays inert (no writes).
    if language_update.is_active is False and language.is_active is True:
        default_refs = await _find_default_schemas(db, language.id)
        if default_refs:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot deactivate language '{language.code}': it is the default language of schema(s): {', '.join(default_refs)}. Re-point or clear the default first.",
            )
    if language_update.name is not None and language.name != language_update.name:
        language.name = language_update.name
    if language_update.is_active is not None and language.is_active != language_update.is_active:
        language.is_active = language_update.is_active
    await audit_log(db, "languages", language_id, "UPDATE", actor_id,
                    old_values=old_values, new_values=model_to_dict(language))
    await db.commit()
    await db.refresh(language)
    return language


async def delete_language(db, language_id: uuid.UUID, actor_id: uuid.UUID) -> bool:
    result = await db.execute(select(Language).where(Language.id == language_id))
    language = result.scalar_one_or_none()
    if not language:
        return False
    # D-02: DELETE is blocked when the language is enabled in ANY schema's subset
    # OR is ANY schema's default. Never silently cascades schema_languages rows.
    subset_schemas = await _find_subset_schemas(db, language.id)
    default_refs = await _find_default_schemas(db, language.id)
    referencing = sorted(set(subset_schemas) | set(default_refs))
    if referencing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot delete language '{language.code}': still referenced by schema(s): {', '.join(referencing)}. Remove it from every schema's subset first.",
        )
    await audit_log(db, "languages", language_id, "DELETE", actor_id,
                    old_values=model_to_dict(language), new_values=None)
    await db.delete(language)
    await db.commit()
    return True


async def _find_subset_schemas(db, language_id: uuid.UUID) -> list[str]:
    """Schema names where this language appears in the enabled subset."""
    result = await db.execute(
        select(Schema.name)
        .join(SchemaLanguage, SchemaLanguage.schema_id == Schema.id)
        .where(SchemaLanguage.language_id == language_id)
    )
    return [r[0] for r in result.all()]


async def _find_default_schemas(db, language_id: uuid.UUID) -> list[str]:
    """Schema names where this language is the default."""
    result = await db.execute(
        select(Schema.name).where(Schema.default_language_id == language_id)
    )
    return [r[0] for r in result.all()]
