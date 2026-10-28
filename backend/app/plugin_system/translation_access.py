"""Core-owned translation scope authorization and neutral projections (D-13..D-16).

The neutral SDK translation boundary treats *actor attribution* (who the
persisted creator is) as a fact, never as an authorization. This module is the
single Core-owned place that answers two questions for a translation scope:

* **Which real Core records does this scope belong to?** (the owning ObjectType,
  AttributeType, Object, Schema — never a plugin-supplied claim)
* **Is the resolved actor allowed to read / update it?** (``translations:read`` /
  ``translations:update`` plus the mapped entity read/update right plus the
  existing deny-wins record cascade)

The plugin only ever receives a bounded, neutral projection
(:class:`TranslationScopeResult`); no Core ORM type, no internal auth resolver
and no record-permission implementation is published. D-15: a read right never
confers an update right, and an AI right alone replaces neither.

Actor resolution reuses Core's own write resolver (``core_data._resolve_write``):
a route call must supply an authenticated user, a task call must NOT supply a
user override and is authorized against the persisted ``Task.creator_id``, and a
hook context is refused. The resolved actor is then re-read from the database so
a missing/deleted/inactive user fails closed with the stable
``actor_unavailable`` code (T-307-01/T-307-03).
"""

import base64
import dataclasses
import json
import uuid
from typing import Iterable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.db import async_session_maker
from app.models import (
    AttributeType,
    AttributeTypeDataType,
    Language,
    ObjectType,
    ObjectTypeAttribute,
    ObjectValue,
    ReferenceType,
    Schema,
    SchemaLanguage,
    Status,
    Translation,
)
from app.models.user import User
from app.plugin_system.core_data import (
    CODE_ACTOR_UNAVAILABLE,
    CODE_NOT_AUTHORIZED,
    CoreDataError,
    CoreDataInvalid,
    CoreDataNotFound,
    _resolve_write,
)
from app.plugin_system.core_data_schemas import (
    TranslationEntityPage,
    TranslationEntityPageRequest,
    TranslationEntityProjection,
)
from app.services.auth_service import resolve_user_permissions
from app.services.record_permission_service import (
    VALID_RECORD_ENTITY_TYPES,
    resolve_record_permission,
)
from app.services.select_option_service import build_option_map, resolve_display_value
from app.services.translation_service import _load_select_option_owners

_STRICT = ConfigDict(extra="forbid")

# T-307-02: the six schema inventory nouns the page adapter supports.
INVENTORY_TRANSLATION_TYPES: tuple[str, ...] = (
    "object_type",
    "object_type_attribute",
    "select_option",
    "object_value",
    "status",
    "reference_type",
)

# The mapped entity resource per data translation type (D-15). ``None`` is the
# schema-less UI scope (``systemui``), which carries only the translation rights.
_DATA_SCOPE_RESOURCES: dict[str, str] = {
    "object_type": "object-types",
    "object_type_attribute": "object-type-attributes",
    "select_option": "object-type-attributes",
    "object_value": "object-values",
    "status": "statuses",
    "reference_type": "reference-types",
}

_DATA_TRANSLATION_TYPES = frozenset(_DATA_SCOPE_RESOURCES)
_ALL_TRANSLATION_TYPES = _DATA_TRANSLATION_TYPES | {"systemui"}


class TranslationScopeRequest(BaseModel):
    """A neutral single-scope authorization/read request (strict, D-14/D-15)."""

    model_config = _STRICT

    translation_type: str = Field(min_length=1, max_length=50)
    entity_id: uuid.UUID
    action: Literal["read", "update"] = "read"


class TranslationRecordOwner(BaseModel):
    """One Core record a scope belongs to (the deny-wins record cascade input)."""

    model_config = _STRICT

    entity_type: str
    record_id: uuid.UUID


class TranslationScopeResult(BaseModel):
    """The bounded outcome of one scope authorization (D-15).

    ``can_update`` is the honest answer to "may this actor save into this
    scope?": it is False for a read-only actor even though the read succeeded,
    so no read ever implies an update right. The neutral field/language/target
    projection (``fields``/``datatype``/multilingual flags and, on a projecting
    read, the schema languages plus raw stored targets) lets a plugin read a
    field scope without importing any Core ORM model.
    """

    model_config = _STRICT

    translation_type: str
    entity_id: uuid.UUID
    action: str
    schema_id: Optional[uuid.UUID] = None
    source_id: Optional[uuid.UUID] = None
    record_owners: list[TranslationRecordOwner] = Field(default_factory=list)
    can_update: bool = False
    fields: dict[str, Optional[str]] = Field(default_factory=dict)
    default_language_code: Optional[str] = None
    enabled_target_languages: list[str] = Field(default_factory=list)
    stored_targets: dict[str, dict[str, str]] = Field(default_factory=dict)
    datatype: Optional[str] = None
    own_multilingual: bool = False
    parent_multilingual: bool = False


@dataclasses.dataclass(frozen=True)
class TranslationActor:
    """The Core-resolved acting user of one translation call (never a claim)."""

    id: uuid.UUID
    is_superuser: bool
    email: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class _Scope:
    translation_type: str
    entity_id: uuid.UUID
    schema_id: uuid.UUID | None
    source_id: uuid.UUID | None
    owners: tuple[tuple[str, uuid.UUID], ...]
    datatype: str | None
    own_multilingual: bool
    parent_multilingual: bool
    fields: dict[str, Optional[str]] = dataclasses.field(default_factory=dict)


async def resolve_translation_actor(ctx, user=None) -> TranslationActor:
    """Resolve the real, active actor for this call or fail closed.

    Reuses Core's write resolver so a hook context, a task-context user override
    and a missing route user are refused with their existing stable codes, then
    re-reads the persisted user row so a deleted/inactive actor never acts.
    """
    actor_id = _resolve_write(ctx, user)
    async with async_session_maker() as session:
        row = await session.get(User, actor_id)
        is_superuser = bool(row.is_superuser) if row is not None else False
        is_active = bool(row.is_active) if row is not None else False
        email = row.email if row is not None else None
    if row is None or not is_active:
        raise CoreDataInvalid(
            "The acting user is missing or inactive",
            code=CODE_ACTOR_UNAVAILABLE,
        )
    return TranslationActor(id=actor_id, is_superuser=is_superuser, email=email)


async def _effective_permissions(actor: TranslationActor) -> frozenset[str]:
    if actor.is_superuser:
        return frozenset()
    return frozenset(await resolve_user_permissions(str(actor.id)))


def _granted(actor: TranslationActor, permissions: frozenset[str], name: str) -> bool:
    return actor.is_superuser or name in permissions


async def _record_denied(
    actor: TranslationActor,
    owners: Iterable[tuple[str, uuid.UUID]],
    action: str,
) -> bool:
    """True when an explicit inherited record DENY applies to any owner (deny wins)."""
    if actor.is_superuser:
        return False
    for entity_type, record_id in owners:
        if entity_type not in VALID_RECORD_ENTITY_TYPES:
            continue
        resolved = await resolve_record_permission(
            user_id=str(actor.id),
            entity_type=entity_type,
            record_id=record_id,
            action=action,
        )
        if resolved is False:
            return True
    return False


async def _schema_source_id(db, schema_id: uuid.UUID | None) -> uuid.UUID | None:
    if schema_id is None:
        return None
    return (
        await db.execute(select(Schema.source_id).where(Schema.id == schema_id))
    ).scalar_one_or_none()


async def _resolve_select_option_owner(db, option_id: uuid.UUID):
    """Resolve the ONE owning attribute of a SELECT option id, or fail closed.

    A malformed/unknown id (no owner) or an ambiguous id (two attributes claim
    the same option UUID) is a typed failure, never an arbitrary pick — the
    record authority derived from the wrong owner would be a silent bypass
    (T-307-01). Returns ``(attribute, authored option value)``.
    """
    owners = await _load_select_option_owners(db)
    key = str(option_id)
    matches: list[tuple[object, Optional[str]]] = []
    for opt_key, (attr, _attr_ml, _parent_ml, _schema) in owners.items():
        if opt_key == key:
            matches.append((attr, _select_option_value(attr, opt_key)))
    if not matches:
        raise CoreDataNotFound("Select option not found")
    if len(matches) > 1:
        raise CoreDataInvalid("Select option owner is ambiguous")
    return matches[0]


def _select_option_value(attribute, option_key: str) -> Optional[str]:
    """The authored value of one option id inside an attribute's structure."""
    for opt in (attribute.structure or {}).get("options", []):
        if str(opt.get("id")) == option_key:
            return opt.get("value")
    return None


def _object_value_base(datatype, attribute, object_value) -> Optional[str]:
    """Authored display text of an object value (the base a plugin translates).

    Mirrors the existing read-path semantics: TEXT -> ``value_simple``, SELECT ->
    resolved option display string, STATUS -> ``value_struct["name"]``.
    """
    if datatype == AttributeTypeDataType.SELECT.value:
        return (
            resolve_display_value(object_value.value_simple, build_option_map(attribute))
            or ""
        )
    if datatype == AttributeTypeDataType.STATUS.value:
        struct = object_value.value_struct or {}
        return str(struct.get("name") or "")
    return object_value.value_simple


async def _resolve_scope(db, translation_type: str, entity_id: uuid.UUID) -> _Scope:
    """Resolve the real Core owner/schema/source of a translation scope."""
    if translation_type == "object_type":
        row = (
            await db.execute(
                select(
                    ObjectType.schema_id,
                    ObjectType.multilingual,
                    ObjectType.name,
                    ObjectType.description,
                ).where(ObjectType.id == entity_id)
            )
        ).one_or_none()
        if row is None:
            raise CoreDataNotFound("Object type not found")
        return _Scope(
            translation_type=translation_type,
            entity_id=entity_id,
            schema_id=row.schema_id,
            source_id=await _schema_source_id(db, row.schema_id),
            owners=(("object_type", entity_id),),
            datatype=None,
            own_multilingual=bool(row.multilingual),
            parent_multilingual=bool(row.multilingual),
            fields={"name": row.name, "description": row.description},
        )

    if translation_type == "object_type_attribute":
        attr = (
            await db.execute(
                select(ObjectTypeAttribute).where(ObjectTypeAttribute.id == entity_id)
            )
        ).scalar_one_or_none()
        if attr is None:
            raise CoreDataNotFound("Object type attribute not found")
        parent = (
            await db.execute(
                select(ObjectType.schema_id, ObjectType.multilingual).where(
                    ObjectType.id == attr.object_type_id
                )
            )
        ).one_or_none()
        if parent is None:
            raise CoreDataNotFound("Object type not found")
        return _Scope(
            translation_type=translation_type,
            entity_id=entity_id,
            schema_id=parent.schema_id,
            source_id=await _schema_source_id(db, parent.schema_id),
            owners=(
                ("object_type", attr.object_type_id),
                ("attribute_type", attr.attribute_type_id),
            ),
            datatype=None,
            own_multilingual=bool(attr.multilingual),
            parent_multilingual=bool(parent.multilingual),
            fields={"name": attr.name, "description": attr.description},
        )

    if translation_type == "select_option":
        attr, option_value = await _resolve_select_option_owner(db, entity_id)
        parent = (
            await db.execute(
                select(ObjectType.schema_id, ObjectType.multilingual).where(
                    ObjectType.id == attr.object_type_id
                )
            )
        ).one_or_none()
        if parent is None:
            raise CoreDataNotFound("Object type not found")
        return _Scope(
            translation_type=translation_type,
            entity_id=entity_id,
            schema_id=parent.schema_id,
            source_id=await _schema_source_id(db, parent.schema_id),
            owners=(
                ("object_type", attr.object_type_id),
                ("attribute_type", attr.attribute_type_id),
            ),
            datatype=None,
            own_multilingual=bool(attr.multilingual),
            parent_multilingual=bool(parent.multilingual),
            fields={"value": option_value},
        )

    if translation_type == "object_value":
        value = (
            await db.execute(
                select(ObjectValue).where(ObjectValue.id == entity_id)
            )
        ).scalar_one_or_none()
        if value is None:
            raise CoreDataNotFound("Object value not found")
        attr = (
            await db.execute(
                select(ObjectTypeAttribute).where(
                    ObjectTypeAttribute.id == value.object_type_attribute_id
                )
            )
        ).scalar_one_or_none()
        if attr is None:
            raise CoreDataNotFound("Object type attribute not found")
        parent = (
            await db.execute(
                select(ObjectType.schema_id, ObjectType.multilingual).where(
                    ObjectType.id == attr.object_type_id
                )
            )
        ).one_or_none()
        if parent is None:
            raise CoreDataNotFound("Object type not found")
        datatype = (
            await db.execute(
                select(AttributeType.data_type).where(
                    AttributeType.id == attr.attribute_type_id
                )
            )
        ).scalar_one_or_none()
        return _Scope(
            translation_type=translation_type,
            entity_id=entity_id,
            schema_id=parent.schema_id,
            source_id=await _schema_source_id(db, parent.schema_id),
            owners=(
                ("object", value.object_id),
                ("attribute_type", attr.attribute_type_id),
            ),
            datatype=datatype,
            own_multilingual=bool(attr.multilingual),
            parent_multilingual=bool(parent.multilingual),
            fields={"value": _object_value_base(datatype, attr, value)},
        )

    if translation_type == "status":
        status_row = (
            await db.execute(
                select(Status.schema_id, Status.name).where(Status.id == entity_id)
            )
        ).one_or_none()
        if status_row is None:
            raise CoreDataNotFound("Status not found")
        return _Scope(
            translation_type=translation_type,
            entity_id=entity_id,
            schema_id=status_row.schema_id,
            source_id=await _schema_source_id(db, status_row.schema_id),
            owners=(("schema", status_row.schema_id),),
            datatype=None,
            own_multilingual=False,
            parent_multilingual=False,
            fields={"name": status_row.name},
        )

    if translation_type == "reference_type":
        ref_row = (
            await db.execute(
                select(
                    ReferenceType.schema_id,
                    ReferenceType.reference,
                    ReferenceType.description,
                ).where(ReferenceType.id == entity_id)
            )
        ).one_or_none()
        if ref_row is None:
            raise CoreDataNotFound("Reference type not found")
        schema_id = ref_row.schema_id
        # A schema-less reference type (schema_id NULL) has no record owner and
        # stays readable/updatable by the type-level rights only (D-03).
        owners: tuple[tuple[str, uuid.UUID], ...] = (
            (("schema", schema_id),) if schema_id is not None else ()
        )
        return _Scope(
            translation_type=translation_type,
            entity_id=entity_id,
            schema_id=schema_id,
            source_id=await _schema_source_id(db, schema_id),
            owners=owners,
            datatype=None,
            own_multilingual=False,
            parent_multilingual=False,
            fields={
                "reference": ref_row.reference,
                "description": ref_row.description,
            },
        )

    if translation_type == "systemui":
        return _Scope(
            translation_type=translation_type,
            entity_id=entity_id,
            schema_id=None,
            source_id=None,
            owners=(),
            datatype=None,
            own_multilingual=False,
            parent_multilingual=False,
        )

    raise CoreDataInvalid(f"Unsupported translation type {translation_type!r}")


async def _authorize_scope(
    db,
    actor: TranslationActor,
    translation_type: str,
    entity_id: uuid.UUID,
    action: str,
    *,
    permissions: frozenset[str] | None = None,
    with_projection: bool = False,
) -> TranslationScopeResult:
    """Authorize one resolved scope for ``action`` (never returns on a denial).

    ``with_projection`` additionally loads the schema's default/enabled target
    languages and this entity's raw stored targets, so a single-scope read is a
    complete neutral projection. A page read authorizes with it off and batches
    the language/target loads itself.
    """
    permissions = (
        permissions if permissions is not None else await _effective_permissions(actor)
    )
    scope = await _resolve_scope(db, translation_type, entity_id)

    resource = _DATA_SCOPE_RESOURCES.get(translation_type)
    if action == "update":
        required = ["translations:update"]
        if resource is not None:
            required.append(f"{resource}:update")
    else:
        required = ["translations:read"]
        if resource is not None:
            required.append(f"{resource}:read")

    missing = [name for name in required if not _granted(actor, permissions, name)]
    if missing:
        raise CoreDataInvalid(
            f"Not authorized to {action} translations for this scope "
            f"(missing {missing[0]})",
            code=CODE_NOT_AUTHORIZED,
        )
    if await _record_denied(actor, scope.owners, action):
        raise CoreDataInvalid(
            f"Not authorized to {action} translations for this scope "
            "(record-level permission denied)",
            code=CODE_NOT_AUTHORIZED,
        )

    # can_update is an honest capability report, computed for both actions.
    can_update = True
    for name in ("translations:update",) + (
        (f"{resource}:update",) if resource is not None else ()
    ):
        if not _granted(actor, permissions, name):
            can_update = False
            break
    if can_update and await _record_denied(actor, scope.owners, "update"):
        can_update = False

    default_language_code: Optional[str] = None
    enabled_target_languages: list[str] = []
    stored_targets: dict[str, dict[str, str]] = {}
    if with_projection:
        default_language_code, enabled_codes = await enabled_languages_for_schema(
            db, scope.schema_id
        )
        enabled_target_languages = [
            code for code in enabled_codes if code != default_language_code
        ]
        stored = await stored_targets_for_entities(db, translation_type, [entity_id])
        stored_targets = stored.get(entity_id, {})

    return TranslationScopeResult(
        translation_type=translation_type,
        entity_id=entity_id,
        action=action,
        schema_id=scope.schema_id,
        source_id=scope.source_id,
        record_owners=[
            TranslationRecordOwner(entity_type=entity_type, record_id=record_id)
            for entity_type, record_id in scope.owners
        ],
        can_update=can_update,
        fields=scope.fields,
        default_language_code=default_language_code,
        enabled_target_languages=enabled_target_languages,
        stored_targets=stored_targets,
        datatype=scope.datatype,
        own_multilingual=scope.own_multilingual,
        parent_multilingual=scope.parent_multilingual,
    )


async def authorize_translation_scope(
    ctx, request: TranslationScopeRequest, *, user=None
) -> TranslationScopeResult:
    """Public neutral scope authorization (D-14/D-15, T-307-01).

    The plugin never passes an actor id: Core resolves the route user or the
    persisted task creator, re-reads the active user row, and returns which real
    records the scope belongs to plus whether the actor may update it.
    """
    if not isinstance(request, TranslationScopeRequest):
        raise CoreDataInvalid("request must be a TranslationScopeRequest instance")
    actor = await resolve_translation_actor(ctx, user)
    async with async_session_maker() as session:
        return await _authorize_scope(
            session,
            actor,
            request.translation_type,
            request.entity_id,
            request.action,
            with_projection=True,
        )


def batch_authorizer(actor: TranslationActor):
    """Build the per-plan authorizer the translation batch write calls before DML.

    A single resolved actor is reused across the batch; the owner reads run on
    the batch's own write session so authorization and the write see one state.
    """
    memo: dict[str, frozenset[str]] = {}

    async def _permissions() -> frozenset[str]:
        if "value" not in memo:
            memo["value"] = await _effective_permissions(actor)
        return memo["value"]

    async def _authorize(db, plan) -> None:
        await _authorize_scope(
            db,
            actor,
            plan.translation_type.code,
            plan.entity_id,
            "update",
            permissions=await _permissions(),
        )

    return _authorize


async def authorize_ui_translation_scope(
    ctx, *, action: str = "read", user=None
) -> TranslationActor:
    """Authorize the schema-less UI (systemui) translation scope (D-15).

    ``systemui`` has no owning Core record, so only the translation right is
    required: ``translations:read`` for a read, ``translations:update`` for a
    write. Returns the resolved actor for reuse as the audit actor.
    """
    actor = await resolve_translation_actor(ctx, user)
    permissions = await _effective_permissions(actor)
    name = "translations:update" if action == "update" else "translations:read"
    if not _granted(actor, permissions, name):
        raise CoreDataInvalid(
            f"Not authorized to {action} UI translations (missing {name})",
            code=CODE_NOT_AUTHORIZED,
        )
    return actor


async def enabled_languages_for_schema(
    db, schema_id: uuid.UUID | None
) -> tuple[Optional[str], list[str]]:
    """The schema's default language code plus its enabled language codes."""
    if schema_id is None:
        return None, []
    default_id = (
        await db.execute(
            select(Schema.default_language_id).where(Schema.id == schema_id)
        )
    ).scalar_one_or_none()
    default_code = None
    if default_id is not None:
        default_code = (
            await db.execute(select(Language.code).where(Language.id == default_id))
        ).scalar_one_or_none()
    codes = (
        (
            await db.execute(
                select(Language.code)
                .join(SchemaLanguage, SchemaLanguage.language_id == Language.id)
                .where(SchemaLanguage.schema_id == schema_id)
            )
        )
        .scalars()
        .all()
    )
    return default_code, sorted(codes)


async def stored_targets_for_entities(
    db, translation_type: str, entity_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, dict[str, str]]]:
    """Raw stored target values: ``{entity_id: {field: {language_code: value}}}``."""
    if not entity_ids:
        return {}
    from app.services.translation_service import get_translation_type_by_code

    translation_type_row = await get_translation_type_by_code(db, translation_type)
    if translation_type_row is None:
        return {}
    rows = (
        await db.execute(
            select(
                Translation.entity_id,
                Translation.field,
                Translation.value,
                Language.code,
            )
            .join(Language, Language.id == Translation.language_id)
            .where(
                Translation.translation_type_id == translation_type_row.id,
                Translation.entity_id.in_(entity_ids),
            )
        )
    ).all()
    out: dict[uuid.UUID, dict[str, dict[str, str]]] = {}
    for entity_id, field, value, code in rows:
        out.setdefault(entity_id, {}).setdefault(field, {})[code] = value
    return out


# --- Neutral schema entity inventory page (D-05/D-15) -------------------------

# The fixed noun order of the one schema-scoped page (D-05). Every candidate is
# ordered by (noun position, entity UUID), so a single opaque cursor traverses
# all six nouns deterministically.
_PAGE_NOUNS: tuple[str, ...] = INVENTORY_TRANSLATION_TYPES
_NOUN_ORDER: dict[str, int] = {noun: index for index, noun in enumerate(_PAGE_NOUNS)}


def _encode_cursor(translation_type: str, entity_id: uuid.UUID) -> str:
    payload = json.dumps([translation_type, str(entity_id)]).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[int, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        translation_type, entity_id = json.loads(raw)
    except (ValueError, TypeError, KeyError):
        raise CoreDataInvalid("Invalid translation entity page cursor") from None
    if not isinstance(translation_type, str) or translation_type not in _NOUN_ORDER:
        raise CoreDataInvalid("Invalid translation entity page cursor")
    try:
        return _NOUN_ORDER[translation_type], uuid.UUID(str(entity_id))
    except (ValueError, TypeError, AttributeError):
        raise CoreDataInvalid("Invalid translation entity page cursor") from None


async def _inventory_candidate_ids(
    db, schema_id: uuid.UUID, *, translation_types: Iterable[str] | None = None
) -> list[tuple[str, uuid.UUID]]:
    """Every inventory entity of one schema, ordered by (noun, UUID).

    A raw inventory: Core decides neither multilingual eligibility nor any AI
    candidate selection — the plugin applies D-05/D-06 from the projected flags.
    ``translation_types`` narrows the queries to the requested nouns only;
    ``None`` means every noun.
    """
    requested = (
        set(_PAGE_NOUNS)
        if translation_types is None
        else {noun for noun in translation_types if noun in _NOUN_ORDER}
    )
    pairs: list[tuple[str, uuid.UUID]] = []

    object_type_nouns = {"object_type", "object_type_attribute", "select_option", "object_value"}
    ot_ids: list[uuid.UUID] = []
    if requested & object_type_nouns:
        ot_ids = (
            (
                await db.execute(
                    select(ObjectType.id).where(ObjectType.schema_id == schema_id)
                )
            )
            .scalars()
            .all()
        )
    if "object_type" in requested:
        for ot_id in ot_ids:
            pairs.append(("object_type", ot_id))

    attribute_nouns = {"object_type_attribute", "select_option", "object_value"}
    attr_rows: list[tuple[uuid.UUID, dict | None]] = []
    if ot_ids and requested & attribute_nouns:
        attr_rows = (
            await db.execute(
                select(ObjectTypeAttribute.id, ObjectTypeAttribute.structure).where(
                    ObjectTypeAttribute.object_type_id.in_(ot_ids)
                )
            )
        ).all()
    seen_options: set[uuid.UUID] = set()
    for attr_id, structure in attr_rows:
        if "object_type_attribute" in requested:
            pairs.append(("object_type_attribute", attr_id))
        if "select_option" not in requested:
            continue
        for opt in (structure or {}).get("options", []):
            try:
                parsed = uuid.UUID(str(opt.get("id")))
            except (ValueError, TypeError, AttributeError):
                continue
            if parsed in seen_options:
                continue
            seen_options.add(parsed)
            pairs.append(("select_option", parsed))
    if attr_rows and "object_value" in requested:
        value_ids = (
            (
                await db.execute(
                    select(ObjectValue.id).where(
                        ObjectValue.object_type_attribute_id.in_(
                            [attr_id for attr_id, _ in attr_rows]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        for value_id in value_ids:
            pairs.append(("object_value", value_id))

    if "status" in requested:
        status_ids = (
            (
                await db.execute(
                    select(Status.id).where(Status.schema_id == schema_id)
                )
            )
            .scalars()
            .all()
        )
        for status_id in status_ids:
            pairs.append(("status", status_id))
    if "reference_type" in requested:
        reference_ids = (
            (
                await db.execute(
                    select(ReferenceType.id).where(ReferenceType.schema_id == schema_id)
                )
            )
            .scalars()
            .all()
        )
        for reference_id in reference_ids:
            pairs.append(("reference_type", reference_id))

    pairs.sort(key=lambda pair: (_NOUN_ORDER[pair[0]], pair[1]))
    return pairs


async def _authorize_read(
    db,
    actor: TranslationActor,
    permissions: frozenset[str],
    translation_type: str,
    entity_id: uuid.UUID,
) -> Optional[TranslationScopeResult]:
    """Authorize one candidate for a read, or ``None`` when it is denied.

    A denial (or a vanished/ambiguous owner) fails closed: the row is skipped
    rather than serialized, and the cursor still advances past it.
    """
    try:
        return await _authorize_scope(
            db,
            actor,
            translation_type,
            entity_id,
            "read",
            permissions=permissions,
        )
    except CoreDataError:
        return None


async def read_translation_entities(
    ctx, request: TranslationEntityPageRequest, *, user=None
) -> TranslationEntityPage:
    """One authorized, keyset-paginated schema entity inventory page (D-05/D-15).

    Each owner is authorized (translations:read + mapped entity read right +
    deny-wins record cascade) BEFORE any authored or stored text is serialized,
    so a denied record never appears in the page. There is deliberately no
    aggregate count — nothing foreign may leak through a number. The neutral
    projection carries authored source values and raw stored targets, never a
    fallback misidentified as a translation.
    """
    if not isinstance(request, TranslationEntityPageRequest):
        raise CoreDataInvalid(
            "request must be a TranslationEntityPageRequest instance"
        )
    actor = await resolve_translation_actor(ctx, user)
    permissions = await _effective_permissions(actor)
    after_key = _decode_cursor(request.after) if request.after else None

    async with async_session_maker() as session:
        candidates = await _inventory_candidate_ids(
            session, request.schema_id, translation_types=request.translation_types
        )
        authorized: list[tuple[str, uuid.UUID, TranslationScopeResult]] = []
        exhausted = True
        for translation_type, entity_id in candidates:
            key = (_NOUN_ORDER[translation_type], entity_id)
            if after_key is not None and key <= after_key:
                continue
            if len(authorized) >= request.limit:
                exhausted = False
                break
            result = await _authorize_read(
                session, actor, permissions, translation_type, entity_id
            )
            if result is None:
                continue
            authorized.append((translation_type, entity_id, result))

        next_cursor: Optional[str] = None
        if not exhausted and authorized:
            next_cursor = _encode_cursor(authorized[-1][0], authorized[-1][1])

        default_language_code, enabled_codes = await enabled_languages_for_schema(
            session, request.schema_id
        )
        enabled_target_languages = [
            code for code in enabled_codes if code != default_language_code
        ]

        ids_by_noun: dict[str, list[uuid.UUID]] = {}
        for translation_type, entity_id, _result in authorized:
            ids_by_noun.setdefault(translation_type, []).append(entity_id)
        stored_by_noun: dict[str, dict] = {}
        for translation_type, ids in ids_by_noun.items():
            stored_by_noun[translation_type] = await stored_targets_for_entities(
                session, translation_type, ids
            )

        items = [
            TranslationEntityProjection(
                translation_type=translation_type,
                entity_id=entity_id,
                schema_id=request.schema_id,
                source_id=result.source_id,
                fields=result.fields,
                default_language_code=default_language_code,
                enabled_target_languages=enabled_target_languages,
                stored_targets=stored_by_noun.get(translation_type, {}).get(
                    entity_id, {}
                ),
                datatype=result.datatype,
                own_multilingual=result.own_multilingual,
                parent_multilingual=result.parent_multilingual,
            )
            for translation_type, entity_id, result in authorized
        ]

    return TranslationEntityPage(items=items, next_cursor=next_cursor)

