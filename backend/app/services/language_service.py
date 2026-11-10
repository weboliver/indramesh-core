import uuid
from typing import List

from fastapi import HTTPException, status
from sqlalchemy import select, func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.data import BUNDLED_LANGUAGE_CODES
from app.models import Language, Schema, SchemaLanguage, UserProfile
from app.plugin_system import get_plugin_registry
from app.plugin_system import hooks as plugin_hooks
from app.schemas.crud import LanguageCreate, LanguageUpdate
from app.services.audit_service import audit_log, model_to_dict
from app.services.cache_service import invalidate_entity_cache


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
    # Bundled languages ship a committed catalog and are part of the
    # application, not operator data. Rejected BEFORE the schema-reference
    # check so the answer is stable (409, row stays) regardless of which
    # schemas happen to reference it.
    if language.code in BUNDLED_LANGUAGE_CODES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot delete language '{language.code}': it is bundled with the application.",
        )
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
    # user_profiles.ui_language carries no FK, so clearing the preference is the
    # only thing that keeps a deleted code from lingering on a profile.
    await db.execute(
        update(UserProfile)
        .where(UserProfile.ui_language == language.code)
        .values(ui_language=None)
    )
    # Captured before the row disappears: the post-commit hook reports the CODE,
    # never the id (a recreated code is a different row).
    code = language.code
    await audit_log(db, "languages", language_id, "DELETE", actor_id,
                    old_values=model_to_dict(language), new_values=None)
    await db.delete(language)
    await db.commit()
    # The UI dictionary is cached per language CODE (not per id) and the cache
    # outlives the row — without this, recreating the same code would serve the
    # deleted language's dictionary.
    await invalidate_entity_cache("ui_translations")
    # D-19: plugins clean up their own rows for this code. Fired AFTER the commit
    # and with no active session, so a subscriber opens its own transaction and a
    # plugin-side failure can never undo or block the language delete.
    await _fire_language_deleted_hook(code, actor_id)
    return True


async def _fire_language_deleted_hook(code: str, actor_id: uuid.UUID) -> None:
    """Fire the generic ``on_language_deleted`` hook (D-19) best-effort.

    Core names no plugin table and imports nothing from ``plugins/``: it only
    announces the deleted code. A missing/uninitialized registry (unit tests
    calling this service without the app lifespan) skips the hook silently, the
    same additive-only rule the object hooks follow. Because hooks never gate,
    the subscriber's own errors are isolated by the hook bus.
    """
    try:
        registry = get_plugin_registry()
    except RuntimeError:
        return
    await plugin_hooks.fire_hook(
        registry,
        "on_language_deleted",
        active_session=None,
        language_code=code,
        actor_id=actor_id,
    )


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
