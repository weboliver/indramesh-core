import dataclasses
import uuid
from typing import Awaitable, Callable, List

from fastapi import HTTPException, status
from sqlalchemy import any_, cast, select, func, and_, delete, or_, update
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PGUUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import (
    Translation, TranslationType, ObjectType, ObjectTypeAttribute,
    Language, Schema, SchemaLanguage, Status, ReferenceType,
    AttributeType, AttributeTypeDataType, Object, ObjectValue, UiString,
    ClassificationLabel,
)
from app.schemas.crud import TranslationUpsert
from app.services.audit_service import audit_log, model_to_dict
from app.services.cache_service import invalidate_entity_cache
from app.services.record_permission_service import build_record_permission_filter
from app.services.select_option_service import option_ids_from_structure
from app.services.value_visibility import resolve_principal_roles

# Field whitelist per translation type (D-12) — name/description for 221 types,
# status name (LOOKUP-01), reference_type reference/description (LOOKUP-02),
# select_option 'value' only (LOOKUP-03, D-10 — matches the canonical structure
# dict key {"id", "value", "position"} so resolution needs no key mapping).
# systemui 'value' only (Phase 230 / STOR-01) — schema-less UI-string storage.
_FIELD_WHITELIST: dict[str, set[str]] = {
    "object_type": {"name", "description"},
    "object_type_attribute": {"name", "description"},
    "status": {"name"},
    "reference_type": {"reference", "description"},
    "select_option": {"value"},
    "object_value": {"value"},
    "systemui": {"value"},
    # D-13: label names only — the family carries the semantics, a label has no
    # description field.
    "classification_label": {"name"},
}

# Types accepted by the /translations endpoint in Phase 221/222/223/224 (D-13/D-20).
# systemui added in Phase 230 / STOR-01. classification_label added in 317.1 (D-13).
_SUPPORTED_TRANSLATION_TYPES: set[str] = {"object_type", "object_type_attribute", "status", "reference_type", "select_option", "object_value", "systemui", "classification_label"}

# Translation types whose entity has no owning schema: the schema-language
# envelope (default-language fallback, multilingual gates) does not apply.
_SCHEMA_LESS_TRANSLATION_TYPES: frozenset[str] = frozenset({"systemui", "classification_label"})

# Shared, single-source cache-invalidation targets per translation type (SEAM-04).
# Both the existing manual REST upsert and the new SDK batch write drop exactly
# this set once per successful call, so the two paths cannot drift.
_INVALIDATION_TAGS: dict[str, tuple[str, ...]] = {
    "object_type": ("object_type", "object_type_attribute"),
    "object_type_attribute": ("object_type", "object_type_attribute"),
    # Status name changes affect attribute structure payloads — drop both.
    "status": ("status", "object_type_attribute"),
    # Reference type dict in attribute payloads stays base-language (D-01 scope).
    "reference_type": ("reference_type",),
    # Option translations render inside attribute structure payloads AND inside
    # object payloads (resolved values) — drop both tag families.
    "select_option": ("object_type_attribute", "object"),
    "object_value": ("object_value", "object"),
    "systemui": ("ui_translations",),
    # Catalogue reads are uncached and attribute/object payloads carry the base
    # label name, so a label rename needs no tag drop (317.1 D-13).
    "classification_label": (),
}


def invalidation_tags_for(translation_type_code: str) -> tuple[str, ...]:
    """The Core cache tags one translation write of this type must drop (D-10)."""
    return _INVALIDATION_TAGS.get(translation_type_code, ())


class SchemaNotMultilingual(ValueError):
    """The owning schema has fewer than two enabled languages (LANG-06/LANG-07).

    Domain error (D-01/D-03): REST callers translate it back to the original
    HTTP 400 with the same detail via :func:`require_schema_multilingual`,
    while the SDK Core-data write adapters map it to ``CoreDataInvalid``
    (``invalid_input``) — no ``HTTPException`` crosses the SDK write boundary.
    """


async def get_translation_type_by_code(db: AsyncSession, code: str) -> TranslationType | None:
    result = await db.execute(select(TranslationType).where(TranslationType.code == code))
    return result.scalar_one_or_none()


async def list_translation_types(db: AsyncSession, skip: int = 0, limit: int = 100) -> tuple[List[TranslationType], int]:
    count_query = select(func.count()).select_from(TranslationType)
    query = select(TranslationType).order_by(TranslationType.code).offset(skip).limit(limit)
    total = (await db.execute(count_query)).scalar()
    items = (await db.execute(query)).scalars().all()
    return list(items), total


async def _resolve_language(db: AsyncSession, code: str) -> Language:
    result = await db.execute(select(Language).where(Language.code == code))
    language = result.scalar_one_or_none()
    if not language:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Language not found: {code}")
    if not language.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Language is inactive: {code}")
    return language


async def _load_select_option_owners(
    db: AsyncSession,
) -> dict[str, tuple[ObjectTypeAttribute, bool, bool, uuid.UUID | None]]:
    """Map option id (str) -> (owning attribute, attr multilingual, parent OT
    multilingual, parent OT schema_id) for EVERY SELECT attribute option.

    `object_type_attributes.structure` is a plain SQLAlchemy `JSON` column —
    `.astext`/JSON-containment operators do NOT exist on it (JSONB-only) and
    the codebase has zero precedent for SQL-level JSON querying of `structure`.
    This lookup therefore scans the (bounded) set of SELECT-type attributes and
    inspects each structure in Python — ONE query over SELECT attributes + ONE
    batched parent-ObjectType query, never one query per option id.
    """
    result = await db.execute(
        select(ObjectTypeAttribute)
        .join(AttributeType)
        .where(AttributeType.data_type == AttributeTypeDataType.SELECT)
    )
    attrs = result.scalars().all()
    if not attrs:
        return {}

    parent_ids = {attr.object_type_id for attr in attrs}
    parent_result = await db.execute(
        select(ObjectType.id, ObjectType.schema_id, ObjectType.multilingual).where(
            ObjectType.id.in_(parent_ids)
        )
    )
    parents = {row.id: row for row in parent_result.all()}

    option_map: dict[str, tuple[ObjectTypeAttribute, bool, bool, uuid.UUID | None]] = {}
    for attr in attrs:
        parent = parents.get(attr.object_type_id)
        parent_multilingual = bool(parent.multilingual) if parent else False
        parent_schema_id = parent.schema_id if parent else None
        for opt in (attr.structure or {}).get("options", []):
            opt_id = opt.get("id")
            if opt_id:
                option_map[str(opt_id)] = (
                    attr,
                    bool(attr.multilingual),
                    parent_multilingual,
                    parent_schema_id,
                )
    return option_map


async def _get_entity_schema_id(db: AsyncSession, translation_type_code: str, entity_id: uuid.UUID) -> uuid.UUID:
    """Return the owning schema_id for an entity, validating the entity exists (404)."""
    if translation_type_code == "object_type":
        result = await db.execute(select(ObjectType.schema_id).where(ObjectType.id == entity_id))
        schema_id = result.scalar_one_or_none()
        if not schema_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object type not found")
        return schema_id
    if translation_type_code == "status":
        result = await db.execute(select(Status.schema_id).where(Status.id == entity_id))
        schema_id = result.scalar_one_or_none()
        if not schema_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Status not found")
        return schema_id
    if translation_type_code == "reference_type":
        result = await db.execute(select(ReferenceType).where(ReferenceType.id == entity_id))
        row = result.scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reference type not found")
        if row.schema_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Reference type '{entity_id}' has no schema: translations not supported",
            )
        return row.schema_id
    if translation_type_code == "select_option":
        # Option ids live inside attribute structures (no entity row) — resolve
        # the owning attribute via the in-Python scan (T-223-07).
        owners = await _load_select_option_owners(db)
        owner = owners.get(str(entity_id))
        if not owner:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Select option not found")
        schema_id = owner[3]
        if not schema_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object type not found")
        return schema_id
    if translation_type_code == "object_value":
        result = await db.execute(select(ObjectValue).where(ObjectValue.id == entity_id))
        object_value = result.scalar_one_or_none()
        if not object_value:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object value not found")
        result = await db.execute(
            select(ObjectTypeAttribute).where(ObjectTypeAttribute.id == object_value.object_type_attribute_id)
        )
        attribute = result.scalar_one_or_none()
        if not attribute:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object type attribute not found")
        result = await db.execute(select(ObjectType).where(ObjectType.id == attribute.object_type_id))
        object_type = result.scalar_one_or_none()
        if not object_type:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object type not found")
        return object_type.schema_id
    # object_type_attribute
    result = await db.execute(
        select(ObjectTypeAttribute.object_type_id).where(ObjectTypeAttribute.id == entity_id)
    )
    ot_id = result.scalar_one_or_none()
    if not ot_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object type attribute not found")
    result = await db.execute(select(ObjectType.schema_id).where(ObjectType.id == ot_id))
    schema_id = result.scalar_one_or_none()
    if not schema_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object type not found")
    return schema_id


async def count_schema_languages(db: AsyncSession, schema_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count()).select_from(SchemaLanguage).where(SchemaLanguage.schema_id == schema_id)
    )
    return int(result.scalar() or 0)


async def check_schema_multilingual(db: AsyncSession, schema_id: uuid.UUID) -> None:
    """227 LANG-06/LANG-07/D-05: raise unless the schema has >=2 enabled languages.

    Domain-level gate (D-01/D-03): raises :class:`SchemaNotMultilingual`, a
    ``ValueError`` — never an ``HTTPException`` — so a Core-data write reached
    through the SDK keeps the typed-error contract. REST callers must use
    :func:`require_schema_multilingual`, which restores the original 400.
    """
    count = await count_schema_languages(db, schema_id)
    if count < 2:
        raise SchemaNotMultilingual(
            f"schema is not multilingual: only {count} language(s) enabled"
        )


async def require_schema_multilingual(db: AsyncSession, schema_id: uuid.UUID) -> None:
    """REST boundary for :func:`check_schema_multilingual`.

    Translates the domain error back to the original HTTP 400 with the same
    detail, so every route keeps its prior status code and message while the
    SDK-facing gate stays free of ``HTTPException`` (D-03).
    """
    try:
        await check_schema_multilingual(db, schema_id)
    except SchemaNotMultilingual as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from None


async def _check_language_enabled(db: AsyncSession, schema_id: uuid.UUID, language_id: uuid.UUID, code: str) -> None:
    """D-11: translation rows always live within the schema's language envelope."""
    result = await db.execute(
        select(SchemaLanguage.id).where(
            SchemaLanguage.schema_id == schema_id,
            SchemaLanguage.language_id == language_id,
        )
    )
    if result.scalar_one_or_none() is None:
        schema_result = await db.execute(select(Schema.name).where(Schema.id == schema_id))
        schema_name = schema_result.scalar_one_or_none() or str(schema_id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Language '{code}' is not enabled for schema '{schema_name}'",
        )


async def validate_read_language(db: AsyncSession, lang_code: str, schema_id: uuid.UUID | None = None) -> None:
    """Read-side ?lang= strictness (223-05 D-13, reuses 221/222 machinery).

    Unknown or inactive code -> 400 "Language not found: {code}" (D-05).
    When the schema scope is known, the language must be in the schema's
    enabled subset -> 400 "Language '{code}' is not enabled for schema ..."
    (D-06 / 222 D-05 strict rule). Unfiltered reads pass ``schema_id=None``
    and resolve per-entity instead (222 D-05 per-item rule).

    Called BEFORE any per-entity resolution so an empty result set never
    skips the validation (the public API + GraphQL surfaces rely on this).
    """
    requested = await _resolve_language(db, lang_code)
    if schema_id is not None:
        await _check_language_enabled(db, schema_id, requested.id, requested.code)


@dataclasses.dataclass
class TranslationEntryPlan:
    """The resolved write context of one validated translation entry (SEAM-04)."""

    translation_type: TranslationType
    entity_id: uuid.UUID
    field: str
    language: Language
    schema_id: uuid.UUID | None
    cache_tags: tuple[str, ...]


class TranslationValidationError(ValueError):
    """A batch entry failed Core's domain validation (SEAM-04, D-12).

    A plain ``ValueError`` so the SDK Core-data adapter maps it to a bounded
    ``CoreDataInvalid`` while the manual REST path (which never raises this)
    keeps its existing HTTP status codes. ``index`` names the offending entry
    when the failure came from a batch.
    """

    def __init__(self, message: str, *, index: int | None = None) -> None:
        super().__init__(
            f"entry {index}: {message}" if index is not None else message
        )
        self.index = index


async def _select_translation_row(
    db: AsyncSession, plan: TranslationEntryPlan
) -> Translation | None:
    result = await db.execute(
        select(Translation).where(
            Translation.translation_type_id == plan.translation_type.id,
            Translation.entity_id == plan.entity_id,
            Translation.field == plan.field,
            Translation.language_id == plan.language.id,
        )
    )
    return result.scalar_one_or_none()


async def validate_translation_entry(
    db: AsyncSession,
    translation_type_code: str,
    entity_id: uuid.UUID,
    field: str,
    language_code: str,
) -> TranslationEntryPlan:
    """Validate ONE translation entry and resolve its write context (D-09..D-11).

    The domain body shared by the manual REST upsert and the SDK batch write:
    type support, field whitelist, entity existence, multilingual gates,
    schema-language envelope and language resolution. It raises the existing
    ``HTTPException``s so REST keeps its status codes; the SDK batch adapts them
    to its typed error (no ``HTTPException`` crosses the SDK boundary).
    """
    # D-13: supported types (Phase 221/222).
    if translation_type_code not in _SUPPORTED_TRANSLATION_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Translation type '{translation_type_code}' not yet supported",
        )
    translation_type = await get_translation_type_by_code(db, translation_type_code)
    if not translation_type:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Translation type '{translation_type_code}' not found")

    # D-12: field whitelist.
    if field not in _FIELD_WHITELIST[translation_type_code]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Field '{field}' is not translatable for translation type '{translation_type_code}'",
        )

    tags = invalidation_tags_for(translation_type_code)

    # Phase 230 / STOR-01: schema-less systemui branch — must return BEFORE any
    # schema gate (D-09 below, _get_entity_schema_id, check_schema_multilingual,
    # _check_language_enabled). ui_strings rows have no owning schema, so
    # reaching those helpers would raise or leak schema-language semantics into
    # UI resolution (research Pitfall #2).
    if translation_type_code == "systemui":
        result = await db.execute(select(UiString).where(UiString.id == entity_id))
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="UI string not found")
        language = await _resolve_language(db, language_code)
        return TranslationEntryPlan(translation_type, entity_id, field, language, None, tags)

    # D-13: the label catalogue is global (no ``schema_id``, D-12) — same
    # schema-less shape as systemui, with an existence check against the
    # catalogue instead of the UI-string table.
    if translation_type_code == "classification_label":
        result = await db.execute(
            select(ClassificationLabel.id).where(ClassificationLabel.id == entity_id)
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Classification label not found",
            )
        language = await _resolve_language(db, language_code)
        return TranslationEntryPlan(translation_type, entity_id, field, language, None, tags)

    # D-09: two-level opt-in gate — only for metadata types (D-04).
    if translation_type_code in ("object_type", "object_type_attribute"):
        if translation_type_code == "object_type":
            result = await db.execute(select(ObjectType).where(ObjectType.id == entity_id))
            entity = result.scalar_one_or_none()
            if not entity:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object type not found")
            if not entity.multilingual:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                    detail="Object type is not multilingual: translations cannot be upserted")
        else:  # object_type_attribute — gated by the parent ObjectType's flag (D-07/D-09)
            result = await db.execute(
                select(ObjectTypeAttribute).where(ObjectTypeAttribute.id == entity_id)
            )
            attribute = result.scalar_one_or_none()
            if not attribute:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object type attribute not found")
            result = await db.execute(select(ObjectType).where(ObjectType.id == attribute.object_type_id))
            parent = result.scalar_one_or_none()
            if not parent or not parent.multilingual:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                    detail="Object type is not multilingual: translations cannot be upserted")
    # D-08: select_option is gated by the OWNING attribute's multilingual flag
    # AND the parent ObjectType's flag — the two-level opt-in carries down.
    elif translation_type_code == "select_option":
        owners = await _load_select_option_owners(db)
        owner = owners.get(str(entity_id))
        if not owner:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Select option not found")
        attribute, attr_multilingual, parent_multilingual, _ = owner
        if not attr_multilingual:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Attribute is not multilingual: option translations cannot be upserted",
            )
        if not parent_multilingual:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="Object type is not multilingual: translations cannot be upserted")
    # V-05: object_value is gated by the owning attribute's multilingual flag,
    # the parent ObjectType's multilingual flag, AND the attribute data type.
    elif translation_type_code == "object_value":
        result = await db.execute(select(ObjectValue).where(ObjectValue.id == entity_id))
        object_value = result.scalar_one_or_none()
        if not object_value:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object value not found")
        result = await db.execute(
            select(ObjectTypeAttribute)
            .where(ObjectTypeAttribute.id == object_value.object_type_attribute_id)
            .options(selectinload(ObjectTypeAttribute.attribute_type))
        )
        attribute = result.scalar_one_or_none()
        if not attribute:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object type attribute not found")
        result = await db.execute(select(ObjectType).where(ObjectType.id == attribute.object_type_id))
        parent = result.scalar_one_or_none()
        if not parent:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object type not found")
        if not attribute.multilingual:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot translate object value: attribute is not multilingual",
            )
        if not parent.multilingual:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot translate object value: parent object type is not multilingual",
            )
        if attribute.attribute_type.data_type not in (
            AttributeTypeDataType.TEXT.value,
            AttributeTypeDataType.SELECT.value,
            AttributeTypeDataType.STATUS.value,
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot translate object value: data type {attribute.attribute_type.data_type} is not translatable",
            )
    # status/reference_type skip D-09 entirely (D-04 — no multilingual flags on these entities)

    # LANG-06 (227 D-01): status/ref translation upserts require the owning
    # schema to be multilingual. Envelope check below stays as the second gate.
    schema_id: uuid.UUID | None = None
    if translation_type_code in ("status", "reference_type"):
        schema_id = await _get_entity_schema_id(db, translation_type_code, entity_id)
        await require_schema_multilingual(db, schema_id)

    # Language resolution + D-11 envelope.
    language = await _resolve_language(db, language_code)
    if schema_id is None:
        schema_id = await _get_entity_schema_id(db, translation_type_code, entity_id)
    await _check_language_enabled(db, schema_id, language.id, language_code)
    return TranslationEntryPlan(
        translation_type, entity_id, field, language, schema_id, tags
    )


async def _insert_translation_row(
    db: AsyncSession,
    plan: TranslationEntryPlan,
    value: str,
    actor_id: uuid.UUID,
    *,
    expect_conflict: bool,
) -> tuple[Translation | None, bool]:
    """Insert one row under a savepoint so a concurrent insert is a skip.

    The savepoint isolates ONLY this flush-only insert; the caller still commits
    the outer transaction exactly once (SEAM-04/D-12). A unique-constraint race
    on the natural key rolls back to the savepoint and reports a skip instead of
    poisoning the whole batch.
    """
    savepoint = await db.begin_nested()
    translation = Translation(
        translation_type_id=plan.translation_type.id,
        entity_id=plan.entity_id,
        field=plan.field,
        language_id=plan.language.id,
        value=value,
    )
    db.add(translation)
    try:
        await db.flush()
    except IntegrityError:
        await savepoint.rollback()
        if expect_conflict:
            return None, False
        raise
    new_values = model_to_dict(translation)
    await audit_log(db, "translations", translation.id, "CREATE", actor_id,
                    old_values=None, new_values=new_values)
    return translation, True


async def apply_translation_row(
    db: AsyncSession,
    plan: TranslationEntryPlan,
    value: str,
    actor_id: uuid.UUID,
    *,
    mode: str = "replace",
    skip_unchanged: bool = False,
) -> tuple[Translation | None, bool]:
    """Write ONE validated entry inside the caller's transaction (flush only).

    ``fill_missing`` writes only an absent/whitespace-only stored value and
    guards the write against a concurrent manual edit with a conditional
    UPDATE plus a savepoint-isolated unique-constraint insert, reporting a skip
    when another writer filled it (T-303-15). ``replace`` overwrites existing
    content; ``skip_unchanged`` makes an identical value a no-op so an unchanged
    SDK write emits no fabricated second audit mutation. The caller owns the
    single commit and the cache invalidation.
    """
    translation = await _select_translation_row(db, plan)

    if mode == "fill_missing":
        if translation is not None:
            if (translation.value or "").strip():
                return translation, False
            # Conditional write: only fill a value that is STILL empty, so a
            # concurrent manual edit between our read and this UPDATE is never
            # clobbered. The row lock held by the UPDATE serializes writers.
            # Capture old_values BEFORE the bulk UPDATE: an ORM-enabled update
            # synchronizes the in-session instance, so reading it after would
            # record old==new in the audit row (WR-03).
            old_values = model_to_dict(translation)
            result = await db.execute(
                update(Translation)
                .where(
                    Translation.id == translation.id,
                    or_(
                        Translation.value.is_(None),
                        func.trim(Translation.value) == "",
                    ),
                )
                .values(value=value, updated_at=func.now())
                .returning(Translation.id)
            )
            if result.scalar_one_or_none() is None:
                await db.refresh(translation)
                return translation, False
            translation.value = value
            new_values = model_to_dict(translation)
            await audit_log(db, "translations", translation.id, "UPDATE", actor_id,
                            old_values=old_values, new_values=new_values)
            return translation, True
        return await _insert_translation_row(
            db, plan, value, actor_id, expect_conflict=True
        )

    # replace mode: only overwrite existing content when explicitly selected.
    if translation is not None:
        if skip_unchanged and (translation.value or "") == value:
            return translation, False
        old_values = model_to_dict(translation)
        translation.value = value
        new_values = model_to_dict(translation)
        await audit_log(db, "translations", translation.id, "UPDATE", actor_id,
                        old_values=old_values, new_values=new_values)
        return translation, True
    return await _insert_translation_row(
        db, plan, value, actor_id, expect_conflict=False
    )


async def upsert_translation(db: AsyncSession, data: TranslationUpsert, actor_id: uuid.UUID) -> Translation:
    """Manual REST natural-key upsert (D-11) with unchanged commit/replace behaviour.

    Delegates validation and the flush-only row write to the shared primitives,
    then commits once, refreshes the language relationship so
    ``TranslationResponse.language_code`` serializes without a lazy load, and
    drops each affected cache tag exactly once — the pre-extraction behaviour.
    """
    plan = await validate_translation_entry(
        db, data.translation_type, data.entity_id, data.field, data.language_code
    )
    translation, _written = await apply_translation_row(
        db, plan, data.value, actor_id, mode="replace"
    )
    await db.commit()
    await db.refresh(translation, ["language"])
    for tag in plan.cache_tags:
        await invalidate_entity_cache(tag)
    return translation


async def apply_translation_batch(
    db: AsyncSession,
    entries: list[dict],
    *,
    mode: str,
    actor_id: uuid.UUID,
    authorize: "Callable[[AsyncSession, TranslationEntryPlan], Awaitable[None]] | None" = None,
) -> dict:
    """Validate a whole batch, write it atomically and commit exactly once (D-12).

    Every entry is validated BEFORE any row is written, so one invalid entry
    leaves zero inserts. Duplicate natural keys inside the input are rejected
    with an indexed input error rather than accidental last-wins. Expected
    skips (a non-empty stored value in ``fill_missing``, an identical value in
    ``replace``) are reported in the result and are NOT invalid input.

    ``authorize`` (D-15/T-307-01) is the caller's per-plan authorization hook.
    It runs for EVERY validated plan BEFORE the first DML, on this same write
    session, so the entity/owner reads and the write cannot drift; a denial
    raises a typed Core error and no row is written or committed.
    """
    if mode not in ("fill_missing", "replace"):
        raise TranslationValidationError(f"unsupported write mode {mode!r}")

    seen: dict[tuple, int] = {}
    for i, entry in enumerate(entries):
        key = (
            entry["translation_type"],
            str(entry["entity_id"]),
            entry["field"],
            entry["language_code"],
        )
        if key in seen:
            raise TranslationValidationError(
                "duplicate natural key in batch "
                f"(same type/entity/field/language as entry {seen[key]})",
                index=i,
            )
        seen[key] = i

    plans: list[TranslationEntryPlan] = []
    for i, entry in enumerate(entries):
        try:
            plan = await validate_translation_entry(
                db,
                entry["translation_type"],
                entry["entity_id"],
                entry["field"],
                entry["language_code"],
            )
        except HTTPException as exc:
            raise TranslationValidationError(str(exc.detail), index=i) from None
        except SchemaNotMultilingual as exc:
            raise TranslationValidationError(str(exc), index=i) from None
        plans.append(plan)

    # D-15: authorize EVERY plan before the first DML. A denial raises a typed
    # Core error and the whole batch writes nothing.
    if authorize is not None:
        for plan in plans:
            await authorize(db, plan)

    results: list[dict] = []
    tags: list[str] = []
    written = 0
    for i, (entry, plan) in enumerate(zip(entries, plans)):
        try:
            _row, was_written = await apply_translation_row(
                db, plan, entry["value"], actor_id, mode=mode, skip_unchanged=True
            )
        except IntegrityError:
            raise TranslationValidationError(
                "translation write conflicted with an existing row", index=i
            ) from None
        if was_written:
            written += 1
            reason = None
            for tag in plan.cache_tags:
                if tag not in tags:
                    tags.append(tag)
        else:
            reason = "existing_translation" if mode == "fill_missing" else "unchanged"
        results.append({
            "index": i,
            "translation_type": entry["translation_type"],
            "entity_id": entry["entity_id"],
            "field": entry["field"],
            "language_code": entry["language_code"],
            "written": was_written,
            "reason": reason,
        })

    await db.commit()
    for tag in tags:
        await invalidate_entity_cache(tag)
    return {
        "mode": mode,
        "total": len(entries),
        "written": written,
        "skipped": len(entries) - written,
        "results": results,
    }


async def read_translation_values(
    db: AsyncSession,
    translation_type_code: str,
    entity_id: uuid.UUID,
    fields: list[str],
    language_code: str,
) -> list[dict]:
    """Read raw stored values + effective/fallback values with provenance (SEAM-04).

    ``stored_value`` is the requested-language row's raw value (``None`` when
    absent), ``effective_value`` falls back to the owning schema's default
    language row, and ``fallback_source`` is one of ``"explicit"``,
    ``"default_language"`` or ``"none"`` — so a non-empty fallback is never
    mistaken for an explicit target-language translation.
    """
    if translation_type_code not in _SUPPORTED_TRANSLATION_TYPES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"Translation type '{translation_type_code}' not yet supported")
    translation_type = await get_translation_type_by_code(db, translation_type_code)
    if not translation_type:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"Translation type '{translation_type_code}' not found")
    for field in fields:
        if field not in _FIELD_WHITELIST[translation_type_code]:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Field '{field}' is not translatable for translation type '{translation_type_code}'",
            )

    language = await _resolve_language(db, language_code)

    schema_id: uuid.UUID | None = None
    if translation_type_code not in _SCHEMA_LESS_TRANSLATION_TYPES:
        schema_id = await _get_entity_schema_id(db, translation_type_code, entity_id)

    default_language_id: uuid.UUID | None = None
    default_language_code: str | None = None
    if schema_id is not None:
        default_language_id = (
            await db.execute(select(Schema.default_language_id).where(Schema.id == schema_id))
        ).scalar_one_or_none()
        if default_language_id is not None:
            default_language_code = (
                await db.execute(select(Language.code).where(Language.id == default_language_id))
            ).scalar_one_or_none()

    lang_ids = {language.id}
    if default_language_id is not None:
        lang_ids.add(default_language_id)
    rows = (await db.execute(
        select(Translation).where(
            Translation.translation_type_id == translation_type.id,
            Translation.entity_id == entity_id,
            Translation.field.in_(fields),
            Translation.language_id.in_(list(lang_ids)),
        )
    )).scalars().all()
    by_field: dict[str, dict[uuid.UUID, str]] = {}
    for t in rows:
        by_field.setdefault(t.field, {})[t.language_id] = t.value

    out: list[dict] = []
    for field in fields:
        field_langs = by_field.get(field, {})
        stored = field_langs.get(language.id)
        if stored is not None:
            effective, source, serving = stored, "explicit", language.code
        elif default_language_id is not None and default_language_id in field_langs:
            effective = field_langs[default_language_id]
            source, serving = "default_language", default_language_code
        else:
            effective, source, serving = None, "none", None
        out.append({
            "field": field,
            "stored_value": stored,
            "effective_value": effective,
            "fallback_source": source,
            "serving_language_code": serving,
        })
    return out


async def _translation_visibility_predicate(
    db: AsyncSession,
    user_id: str | None,
    is_superuser: bool,
):
    """The ONE record-visibility predicate the translations list applies (AUTHZ-06).

    Ownership mirrors ``plugin_system/translation_access._resolve_scope``: each
    record-owned translation type resolves its owner inside the owner's OWN
    table (``Translation.entity_id`` carries the Status / ReferenceType id,
    never a Schema id) and is admitted through the existing record-permission
    SQL fragments. ``systemui`` and ``classification_label`` have no record
    owner and stay unfiltered.

    Returns ``None`` when nothing has to be filtered — no principal supplied, a
    superuser, or a principal whose four fragments are all unrestricted — so an
    authorized caller receives exactly the rows it received before.

    The fragments are SQL, never a per-row ``resolve_record_permission`` loop:
    the same predicate object is applied to the page query AND the count query,
    and a new DENY takes effect on the next request (no 60 s single-row cache).
    """
    if not user_id or is_superuser:
        return None

    role_ids = await resolve_principal_roles(db, user_id, False)
    fragments = {
        entity_type: await build_record_permission_filter(
            db, user_id, entity_type, is_superuser=False, role_ids=role_ids
        )
        for entity_type in ("schema", "object_type", "attribute_type", "object")
    }
    if all(fragment is None for fragment in fragments.values()):
        return None

    schema_clause = fragments["schema"]
    object_type_clause = fragments["object_type"]
    attribute_type_clause = fragments["attribute_type"]
    object_clause = fragments["object"]

    type_ids: dict[str, uuid.UUID] = dict(
        (await db.execute(select(TranslationType.code, TranslationType.id))).all()
    )

    arms = []

    def _scoped(stmt, *clauses):
        """Apply only the fragments that actually restrict (``None`` = open)."""
        restricting = [clause for clause in clauses if clause is not None]
        if not restricting:
            return None
        for clause in restricting:
            stmt = stmt.where(clause)
        return stmt

    def _admit(code: str, owner_stmt) -> None:
        """One type arm: the code, narrowed to a visible-owner subquery if any."""
        type_id = type_ids.get(code)
        if type_id is None:
            return
        if owner_stmt is None:
            arms.append(Translation.translation_type_id == type_id)
        else:
            arms.append(and_(
                Translation.translation_type_id == type_id,
                Translation.entity_id.in_(owner_stmt),
            ))

    # object_type: the translation's entity IS the ObjectType.
    _admit("object_type", _scoped(select(ObjectType.id), object_type_clause))

    # object_type_attribute: owning ObjectType AND owning AttributeType.
    _admit("object_type_attribute", _scoped(
        select(ObjectTypeAttribute.id)
        .join(ObjectType, ObjectTypeAttribute.object_type_id == ObjectType.id)
        .join(AttributeType, ObjectTypeAttribute.attribute_type_id == AttributeType.id),
        object_type_clause,
        attribute_type_clause,
    ))

    # object_value: owning Object AND owning AttributeType.
    _admit("object_value", _scoped(
        select(ObjectValue.id)
        .join(
            ObjectTypeAttribute,
            ObjectValue.object_type_attribute_id == ObjectTypeAttribute.id,
        )
        .join(AttributeType, ObjectTypeAttribute.attribute_type_id == AttributeType.id)
        .join(Object, ObjectValue.object_id == Object.id),
        object_clause,
        attribute_type_clause,
    ))

    # status / reference_type: entity_id is the Status / ReferenceType id, so the
    # owning schema is resolved inside the entity's OWN table — comparing
    # entity_id against a Schema id would drop every row of these two types.
    if schema_clause is None:
        _admit("status", None)
        _admit("reference_type", None)
    else:
        _admit("status", select(Status.id).where(
            Status.schema_id.in_(select(Schema.id).where(schema_clause))
        ))
        # A schema-less ReferenceType has no record owner at all — never hidden.
        _admit("reference_type", select(ReferenceType.id).where(or_(
            ReferenceType.schema_id.is_(None),
            ReferenceType.schema_id.in_(select(Schema.id).where(schema_clause)),
        )))

    # select_option: the option id lives inside object_type_attributes.structure
    # (there is no row of its own), so derive the denied owners first: every
    # scanned SELECT attribute that the visible-owner set does not contain is
    # denied (deny wins, and no not_() SQL is involved — a NULL in the ancestor
    # join can therefore never quietly keep a denied owner).
    denied_option_ids: list[uuid.UUID] = []
    if "select_option" in type_ids:
        scanned = (await db.execute(
            select(ObjectTypeAttribute.id, ObjectTypeAttribute.structure)
            .join(AttributeType)
            .where(AttributeType.data_type == AttributeTypeDataType.SELECT)
        )).all()
        visible_owner_stmt = _scoped(
            select(ObjectTypeAttribute.id)
            .join(ObjectType, ObjectTypeAttribute.object_type_id == ObjectType.id)
            .join(AttributeType, ObjectTypeAttribute.attribute_type_id == AttributeType.id),
            object_type_clause,
            attribute_type_clause,
        )
        visible_owner_ids = (
            {row[0] for row in scanned}
            if visible_owner_stmt is None
            else {row[0] for row in (await db.execute(visible_owner_stmt)).all()}
        )
        for owner_id, structure in scanned:
            if owner_id in visible_owner_ids:
                continue
            for option in (structure or {}).get("options", []):
                option_id = option.get("id")
                if not option_id:
                    continue
                try:
                    denied_option_ids.append(uuid.UUID(str(option_id)))
                except (TypeError, ValueError):
                    continue

    # No record owner exists for these — always visible, never filtered (D-05).
    _admit("select_option", None)
    _admit("systemui", None)
    _admit("classification_label", None)

    # A code seeded after this plan keeps its previous (unfiltered) behaviour
    # instead of disappearing for every restricted caller.
    known_codes = {
        "object_type", "object_type_attribute", "object_value", "status",
        "reference_type", "select_option", "systemui", "classification_label",
    }
    for code, type_id in type_ids.items():
        if code not in known_codes:
            arms.append(Translation.translation_type_id == type_id)

    if not arms:
        return None

    predicate = or_(*arms)
    if denied_option_ids:
        # AND over the type-arm OR, never one more OR arm: as an arm it would
        # admit every row whose entity_id is not a denied option id.
        predicate = and_(
            predicate, Translation.entity_id.notin_(denied_option_ids)
        )
    return predicate


async def list_translations(
    db: AsyncSession,
    translation_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    field: str | None = None,
    language_code: str | None = None,
    skip: int = 0,
    limit: int = 100,
    *,
    user_id: str | None = None,
    is_superuser: bool = False,
) -> tuple[List[Translation], int]:
    """List translations, filtered to the records the caller may read (AUTHZ-06).

    ``user_id`` / ``is_superuser`` are keyword-only and default to the previous
    behaviour (no record filter), so an existing caller keeps its rows. The
    route supplies the principal; the composed visibility predicate is attached
    to BOTH the page query and the count query, so ``X-Total-Count`` can never
    keep counting denied rows.
    """
    count_query = select(func.count()).select_from(Translation)
    query = select(Translation).options(
        selectinload(Translation.language), selectinload(Translation.translation_type)
    )
    if translation_type:
        tt = await get_translation_type_by_code(db, translation_type)
        if not tt:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Translation type '{translation_type}' not found")
        query = query.where(Translation.translation_type_id == tt.id)
        count_query = count_query.where(Translation.translation_type_id == tt.id)
    visibility = await _translation_visibility_predicate(db, user_id, is_superuser)
    if visibility is not None:
        query = query.where(visibility)
        count_query = count_query.where(visibility)
    if entity_id:
        query = query.where(Translation.entity_id == entity_id)
        count_query = count_query.where(Translation.entity_id == entity_id)
    if field:
        query = query.where(Translation.field == field)
        count_query = count_query.where(Translation.field == field)
    if language_code:
        lang = await _resolve_language(db, language_code)
        query = query.where(Translation.language_id == lang.id)
        count_query = count_query.where(Translation.language_id == lang.id)
    total = (await db.execute(count_query)).scalar()
    items = (await db.execute(query.order_by(Translation.created_at).offset(skip).limit(limit))).scalars().all()
    return list(items), total


async def resolve_translations(
    db: AsyncSession,
    translation_type_code: str,
    entity_ids: list[uuid.UUID],
    fields: list[str],
    lang_code: str,
    schema_id: uuid.UUID | None = None,
) -> dict[uuid.UUID, dict[str, tuple[str, str]]]:
    """Batched translation resolution for read endpoints (MULTI-05).

    Returns {entity_id: {field: (value, serving_language_code)}} — only values
    SOURCED FROM translation rows (base-column fallback is the caller's job).
    serving_language_code is the code of the language the value is actually in:
    the requested code when a translation matched, else the schema's default
    language code (when the default-language translation row exists), else the
    base column is used by the caller with serving code = default code or None.
    Resolution is gated by the entity's multilingual flag (D-10 dormant rule):
    entities whose flag is off yield NO entries (caller serves base values).

    Status and reference_type have no multilingual flag (D-04) — all
    eligible rows enter resolution. Schema-less reference types (schema_id
    NULL) yield no entries per D-03; caller serves base values without
    resolved_language.

    select_option (D-08/D-09): only options whose owning attribute AND parent
    ObjectType are multilingual resolve — flag-off owners are dormant and
    yield NO entries (caller serves base structure values).

    D-05 (always): unknown lang_code → 400 "Language not found: {code}".
    D-06 (when schema_id provided): lang must be in that schema's enabled
    subset → 400 "Language '{code}' is not enabled for this schema".
    """
    if not entity_ids or not fields:
        return {}
    translation_type = await get_translation_type_by_code(db, translation_type_code)
    if not translation_type:
        return {}

    # D-05: requested language must exist (and be active — inactive languages can
    # never be enabled per Phase 220 D-08, so they fall under the D-06 rule).
    requested = await _resolve_language(db, lang_code)
    requested_id = requested.id
    requested_code = requested.code

    # D-06: strict schema-scope check when the caller knows the schema
    # (single-entity GET, schema-filtered list). Unfiltered lists pass None and
    # resolve per-entity with the D-03 fallback (CONTEXT discretion).
    if schema_id is not None:
        await _check_language_enabled(db, schema_id, requested_id, requested_code)

    # Load gate flags + schema ids in ONE batched query per entity kind.
    schema_by_entity: dict[uuid.UUID, uuid.UUID] = {}
    if translation_type_code == "object_type":
        result = await db.execute(
            select(ObjectType.id, ObjectType.schema_id, ObjectType.multilingual).where(ObjectType.id.in_(entity_ids))
        )
        for row in result.all():
            # D-10: flag-off entities are dormant — no translation rows served.
            if row.multilingual:
                schema_by_entity[row.id] = row.schema_id

    elif translation_type_code == "status":
        # D-04: no multilingual flag on Status — all rows eligible.
        # Status.schema_id is NOT NULL by model.
        result = await db.execute(
            select(Status.id, Status.schema_id).where(Status.id.in_(entity_ids))
        )
        for row in result.all():
            schema_by_entity[row.id] = row.schema_id

    elif translation_type_code == "reference_type":
        # D-03: schema-less reference types (schema_id NULL) are skipped —
        # resolution yields NO entries for them (caller serves base values,
        # no resolved_language). Schema-scoped rows resolve normally.
        result = await db.execute(
            select(ReferenceType.id, ReferenceType.schema_id).where(ReferenceType.id.in_(entity_ids))
        )
        for row in result.all():
            if row.schema_id is not None:
                schema_by_entity[row.id] = row.schema_id

    elif translation_type_code == "select_option":
        # D-08/D-09: only options whose owning attribute AND parent ObjectType
        # are multilingual resolve; flag-off owners are dormant (no entries).
        # One bounded scan over SELECT attributes (T-223-07) — never a query
        # per option id.
        owners = await _load_select_option_owners(db)
        wanted = {str(eid) for eid in entity_ids}
        for opt_id, (_, attr_multilingual, parent_multilingual, parent_schema_id) in owners.items():
            if (
                opt_id in wanted
                and attr_multilingual
                and parent_multilingual
                and parent_schema_id is not None
            ):
                schema_by_entity[uuid.UUID(opt_id)] = parent_schema_id

    elif translation_type_code == "object_value":
        # V-05: only object values whose owning attribute AND parent ObjectType
        # are multilingual AND whose attribute data type is TEXT/SELECT/STATUS
        # resolve; flag-off or ineligible rows are dormant (no entries).
        result = await db.execute(
            select(ObjectValue).where(ObjectValue.id.in_(entity_ids))
        )
        values = {row.id: row for row in result.scalars().all()}
        distinct_attr_ids = {row.object_type_attribute_id for row in values.values()}
        attr_result = await db.execute(
            select(ObjectTypeAttribute)
            .where(ObjectTypeAttribute.id.in_(distinct_attr_ids))
            .options(selectinload(ObjectTypeAttribute.attribute_type))
        )
        attrs = {attr.id: attr for attr in attr_result.scalars().all()}
        distinct_ot_ids = {
            attrs[ov.object_type_attribute_id].object_type_id
            for ov in values.values()
            if ov.object_type_attribute_id in attrs
        }
        parent_result = await db.execute(
            select(ObjectType.id, ObjectType.schema_id, ObjectType.multilingual).where(
                ObjectType.id.in_(distinct_ot_ids)
            )
        )
        parents = {row.id: row for row in parent_result.all()}
        for ov in values.values():
            attr = attrs.get(ov.object_type_attribute_id)
            if not attr or not attr.attribute_type:
                continue
            parent = parents.get(attr.object_type_id)
            if not parent:
                continue
            if (
                attr.multilingual
                and parent.multilingual
                and attr.attribute_type.data_type
                in (
                    AttributeTypeDataType.TEXT.value,
                    AttributeTypeDataType.SELECT.value,
                    AttributeTypeDataType.STATUS.value,
                )
            ):
                schema_by_entity[ov.id] = parent.schema_id

    else:  # object_type_attribute — existing branch, verbatim
        result = await db.execute(
            select(ObjectTypeAttribute.id, ObjectTypeAttribute.object_type_id).where(ObjectTypeAttribute.id.in_(entity_ids))
        )
        attr_parent: dict[uuid.UUID, uuid.UUID] = {}
        for row in result.all():
            attr_parent[row.id] = row.object_type_id
        if attr_parent:
            parent_result = await db.execute(
                select(ObjectType.id, ObjectType.schema_id, ObjectType.multilingual).where(
                    ObjectType.id.in_(set(attr_parent.values()))
                )
            )
            parent_schema: dict[uuid.UUID, uuid.UUID] = {}
            gated_ids: set[uuid.UUID] = set()
            for row in parent_result.all():
                parent_schema[row.id] = row.schema_id
                if row.multilingual:
                    gated_ids.add(row.id)
            for attr_id, parent_id in attr_parent.items():
                if parent_id in gated_ids and parent_id in parent_schema:
                    schema_by_entity[attr_id] = parent_schema[parent_id]

    if not schema_by_entity:
        return {}

    # LANG-06 read gate (227 D-01): while the owning schema has fewer than 2
    # enabled languages, NO branch serves translation content — rows stay
    # stored and dormant. One batched grouped count; applies uniformly to
    # object_type, object_type_attribute, status, reference_type,
    # select_option and object_value branches (CONTEXT: no fast path needed).
    multilingual_schema_ids = {
        row.schema_id
        for row in (
            await db.execute(
                select(SchemaLanguage.schema_id, func.count())
                .where(SchemaLanguage.schema_id.in_(
                    [sid for sid in schema_by_entity.values() if sid is not None]
                ))
                .group_by(SchemaLanguage.schema_id)
            )
        ).all()
        if row[1] >= 2
    }
    schema_by_entity = {
        eid: sid for eid, sid in schema_by_entity.items()
        if sid in multilingual_schema_ids
    }
    if not schema_by_entity:
        return {}

    # Batched: schema default languages + enabled subsets (2 queries max).
    schema_ids = list({sid for sid in schema_by_entity.values()})
    schema_result = await db.execute(
        select(Schema.id, Schema.default_language_id).where(Schema.id.in_(schema_ids))
    )
    default_lang_by_schema: dict[uuid.UUID, uuid.UUID | None] = dict(schema_result.all())

    # Requested language + all default languages in one IN query.
    default_ids = {
        default_lang_by_schema[sid]
        for sid in default_lang_by_schema
        if default_lang_by_schema[sid] is not None
    }
    lang_ids = {requested_id} | {lid for lid in default_ids if lid is not None}
    lang_result = await db.execute(select(Language.code, Language.id).where(Language.id.in_(lang_ids)))
    code_by_id = {lid: code for code, lid in lang_result.all()}

    translations_result = await db.execute(
        select(Translation).options(selectinload(Translation.language)).where(
            Translation.translation_type_id == translation_type.id,
            Translation.entity_id.in_(list(schema_by_entity.keys())),
            Translation.field.in_(fields),
            Translation.language_id.in_(lang_ids),
        )
    )
    by_entity: dict[uuid.UUID, dict[str, dict[uuid.UUID, str]]] = {}
    for t in translations_result.scalars().all():
        by_entity.setdefault(t.entity_id, {}).setdefault(t.field, {})[t.language_id] = t.value

    out: dict[uuid.UUID, dict[str, tuple[str, str]]] = {}
    for entity_id, schema_id in schema_by_entity.items():
        default_id = default_lang_by_schema.get(schema_id)
        entity_fields = by_entity.get(entity_id, {})
        resolved: dict[str, tuple[str, str]] = {}
        for field in fields:
            field_langs = entity_fields.get(field, {})
            if requested_id in field_langs:
                resolved[field] = (field_langs[requested_id], requested_code)
            elif default_id is not None and default_id in field_langs:
                resolved[field] = (field_langs[default_id], code_by_id[default_id])
            # else: caller serves the base column; serving code = default code or None
        if resolved:
            out[entity_id] = resolved
    return out


async def delete_systemui_translation(
    db: AsyncSession, ui_string_id: uuid.UUID, language_code: str, actor_id: uuid.UUID
) -> bool:
    """Idempotent, audited reset for a single systemui/value/language row
    (Phase 233 / ADMIN-02/03). Validates the registry key and active
    language, targets exactly the systemui/entity/value/language natural
    key, audit-logs DELETE before removal, commits, then invalidates the
    ui_translations dictionary cache tag ONLY when a row was actually
    removed. Never reuses delete_translations_for() (transaction-scoped
    owner cleanup for a different call site) and never persists a blank
    value row -- the no-persistent-empty-row invariant is upheld by the
    caller never storing "" in the first place.
    """
    result = await db.execute(select(UiString).where(UiString.id == ui_string_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="UI string not found")

    language = await _resolve_language(db, language_code)
    translation_type = await get_translation_type_by_code(db, "systemui")
    if not translation_type:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="systemui translation type is not seeded")

    result = await db.execute(
        select(Translation).where(
            Translation.translation_type_id == translation_type.id,
            Translation.entity_id == ui_string_id,
            Translation.field == "value",
            Translation.language_id == language.id,
        )
    )
    translation = result.scalar_one_or_none()
    if not translation:
        return False

    old_values = model_to_dict(translation)
    await audit_log(db, "translations", translation.id, "DELETE", actor_id,
                    old_values=old_values, new_values=None)
    await db.delete(translation)
    await db.commit()
    await invalidate_entity_cache("ui_translations")
    return True


def _uuid_any(ids):
    """``= ANY`` with one array parameter: ``IN`` binds every id, and asyncpg
    refuses a statement with more than 32,767 parameters."""
    return any_(cast(list(ids), ARRAY(PGUUID(as_uuid=True))))


async def delete_translations_for(
    db: AsyncSession,
    translation_type_code: str,
    entity_ids: list[uuid.UUID],
) -> None:
    """Delete all Translation rows for the given type code + entity ids (D-15).

    Runs inside the caller's transaction (no commit here) — must be called
    BEFORE the caller's db.commit() in every owning delete path.
    """
    if not entity_ids:
        return
    translation_type = await get_translation_type_by_code(db, translation_type_code)
    if not translation_type:
        return
    await db.execute(
        delete(Translation).where(
            Translation.translation_type_id == translation_type.id,
            Translation.entity_id == _uuid_any(entity_ids),
        )
    )


async def collect_dependent_translation_ids(
    db: AsyncSession,
    *,
    source_id: uuid.UUID | None = None,
    schema_ids=(),
    object_type_ids=(),
    attribute_ids=(),
    object_ids=(),
) -> dict[str, set[uuid.UUID]]:
    """Translation code to entity ids of everything a delete in this scope removes.

    Pure read, and it must run before the delete: ``translations.entity_id`` has
    no foreign key, so afterwards the rows that lead to the ids are gone. A
    schema delete passes no ``source_id``, so source-level reference types stay.
    """
    collected: dict[str, set[uuid.UUID]] = {
        "object_type": set(),
        "object_type_attribute": set(),
        "select_option": set(),
        "object_value": set(),
        "status": set(),
        "reference_type": set(),
    }

    schemas = set(schema_ids)
    if source_id is not None:
        schemas.update(
            (await db.execute(select(Schema.id).where(Schema.source_id == source_id))).scalars()
        )

    if schemas:
        collected["status"].update(
            (await db.execute(select(Status.id).where(Status.schema_id == _uuid_any(schemas)))).scalars()
        )

    reference_scope = []
    if source_id is not None:
        reference_scope.append(ReferenceType.source_id == source_id)
    if schemas:
        reference_scope.append(ReferenceType.schema_id == _uuid_any(schemas))
    if reference_scope:
        collected["reference_type"].update(
            (await db.execute(select(ReferenceType.id).where(or_(*reference_scope)))).scalars()
        )

    types = set(object_type_ids)
    if schemas:
        types.update(
            (
                await db.execute(
                    select(ObjectType.id).where(ObjectType.schema_id == _uuid_any(schemas))
                )
            ).scalars()
        )
    # parent_id is ON DELETE CASCADE and a child may live in another schema.
    frontier = set(types)
    while frontier:
        children = set(
            (
                await db.execute(
                    select(ObjectType.id).where(ObjectType.parent_id == _uuid_any(frontier))
                )
            ).scalars()
        )
        frontier = children - types
        types |= frontier
    collected["object_type"] = types

    attributes = set(attribute_ids)
    attribute_scope = []
    if attributes:
        attribute_scope.append(ObjectTypeAttribute.id == _uuid_any(attributes))
    if types:
        attribute_scope.append(ObjectTypeAttribute.object_type_id == _uuid_any(types))
        # referenced_object_type_id is ON DELETE CASCADE too: a reference attribute
        # of a surviving type dies with the type it points at.
        attribute_scope.append(ObjectTypeAttribute.referenced_object_type_id == _uuid_any(types))
    if attribute_scope:
        rows = (
            await db.execute(
                select(ObjectTypeAttribute.id, ObjectTypeAttribute.structure).where(
                    or_(*attribute_scope)
                )
            )
        ).all()
        attributes.update(row.id for row in rows)
        for row in rows:
            collected["select_option"].update(option_ids_from_structure(row.structure))
    collected["object_type_attribute"] = attributes

    value_scope = []
    if object_ids:
        value_scope.append(ObjectValue.object_id == _uuid_any(object_ids))
    if attributes:
        value_scope.append(ObjectValue.object_type_attribute_id == _uuid_any(attributes))
    if types:
        value_scope.append(Object.object_type_id == _uuid_any(types))
    if schemas:
        value_scope.append(Object.schema_id == _uuid_any(schemas))
    if value_scope:
        # Starting from translations bounds the result by what will be deleted,
        # not by the size of the tree.
        collected["object_value"].update(
            (
                await db.execute(
                    select(Translation.entity_id)
                    .join(TranslationType, Translation.translation_type_id == TranslationType.id)
                    .join(ObjectValue, ObjectValue.id == Translation.entity_id)
                    .join(Object, Object.id == ObjectValue.object_id)
                    .where(TranslationType.code == "object_value", or_(*value_scope))
                )
            ).scalars()
        )

    return collected


async def delete_dependent_translations(
    db: AsyncSession,
    *,
    source_id: uuid.UUID | None = None,
    schema_ids=(),
    object_type_ids=(),
    attribute_ids=(),
    object_ids=(),
) -> None:
    """Delete the translations of everything a delete in this scope removes.

    Runs inside the caller's transaction (no commit here) and must be called
    before the delete. ``systemui`` and ``classification_label`` have no cascade
    parent here, so they are never handled.
    """
    collected = await collect_dependent_translation_ids(
        db,
        source_id=source_id,
        schema_ids=schema_ids,
        object_type_ids=object_type_ids,
        attribute_ids=attribute_ids,
        object_ids=object_ids,
    )
    for translation_type_code, entity_ids in collected.items():
        await delete_translations_for(db, translation_type_code, list(entity_ids))
