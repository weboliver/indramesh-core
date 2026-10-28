import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import List

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, func, text
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ObjectValue, Object, ObjectTypeAttribute, AttributeType, AttributeTypeDataType
from app.schemas.crud import ObjectValueCreate, ObjectValueUpdate, ObjectValueResponse
from app.validators import sanitize_textarea_html
from app.services.cache_service import (
    build_cache_key,
    build_list_cache_key,
    build_tag_key,
    get_cached,
    set_cached_with_tags,
    invalidate_entity_cache,
    CACHE_TTL_LIST,
    CACHE_TTL_ENTITY,
)
from app.models.index_outbox import IndexOutbox
from app.services.object_service import (
    _enqueue_index_job,
    _fire_object_hook,
    _new_outbox_entry,
    _resolve_origin,
    _touch_system_attribute,
)
from app.services.audit_service import audit_log, model_to_dict, resolve_select_option_display
from app.services.select_option_service import build_option_map, resolve_display_value
from app.services.translation_service import delete_translations_for, resolve_translations
from app.services.value_lock_service import assert_value_writable
from app.services.value_visibility import visible_value_conditions
from app.services import hook_origin

ENTITY_TYPE = "object_value"

# 246 D-04/D-05: which system OTAs are valueless bindings and which Object
# column they bind to. Name -> Object.name; Key -> objects.key (both stay
# valueless — no ObjectValue row is ever stored for them).
_SYSTEM_BOUND_FIELDS = {"Name": "name", "Key": "key"}


def _build_synthetic_system_value(
    obj: Object, attribute: ObjectTypeAttribute, value_simple: str | None = None
) -> ObjectValue:
    """246 D-04/D-05: build an in-memory (never persisted) ObjectValue that
    represents the valueless system Name/Key binding for API responses.

    The system Name OTA binds to Object.name and the system Key OTA binds to
    objects.key — no ObjectValue row is stored (D-04/D-05), but the read and
    write paths must still return a well-formed value payload so the frontend
    grid renders them as normal attributes.
    """
    bound_field = _SYSTEM_BOUND_FIELDS.get(attribute.name)
    bound_value = getattr(obj, bound_field) if bound_field else None
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    synthetic = ObjectValue(
        id=uuid.uuid4(),
        object_id=obj.id,
        object_type_attribute_id=attribute.id,
        value_simple=value_simple if value_simple is not None else bound_value,
        created_at=now,
        updated_at=now,
    )
    synthetic.object_type_attribute = attribute
    return synthetic


async def synthesize_system_bindings(
    db: AsyncSession,
    object_ids: set[uuid.UUID],
    items: list[ObjectValue],
) -> list[ObjectValue]:
    """246 D-04/D-05 read-path synthesis: append synthetic (in-memory, never
    persisted) ObjectValue rows for system Name/Key bindings so the frontend
    grid renders them as normal attributes without a stored row.

    Given the requested object_ids and the already-loaded ObjectValue rows,
    loads each object's own-type system Name/Key OTAs and appends one
    synthetic ObjectValue per (object, OTA) pair that has no stored row.
    """
    if not object_ids:
        return items

    objs_result = await db.execute(select(Object).where(Object.id.in_(object_ids)))
    objs = {o.id: o for o in objs_result.scalars().all()}
    if not objs:
        return items

    type_ids = {o.object_type_id for o in objs.values()}
    ota_result = await db.execute(
        select(ObjectTypeAttribute).where(
            ObjectTypeAttribute.object_type_id.in_(type_ids),
            ObjectTypeAttribute.is_system.is_(True),
            ObjectTypeAttribute.name.in_(list(_SYSTEM_BOUND_FIELDS)),
        )
    )
    otas_by_type: dict[uuid.UUID, list[ObjectTypeAttribute]] = {}
    for ota in ota_result.scalars().all():
        otas_by_type.setdefault(ota.object_type_id, []).append(ota)

    covered = {(v.object_id, v.object_type_attribute_id) for v in items}
    out = list(items)
    for obj_id, obj in objs.items():
        for ota in otas_by_type.get(obj.object_type_id, []):
            if (obj_id, ota.id) in covered:
                continue
            out.append(_build_synthetic_system_value(obj, ota))
    return out


def _stage_object_name_sync(
    db: AsyncSession,
    obj: Object,
    new_name: str,
    *,
    actor_id: uuid.UUID | None = None,
    origin: hook_origin.MutationOrigin | None = None,
    emit_index_outbox: bool = True,
) -> IndexOutbox | None:
    """Stage the Object.name mirror and its index outbox row WITHOUT committing.

    The "Name" attribute is the object's label — EditObjectModal.jsx and
    filter_utils.py's build_filter_clause already treat it that way. The label
    is *formed from* this attribute, not the other way around, so a real value
    change must update Object.name to match.

    CORESEAM-11/D-04/D-05 (291.2-02): the caller owns the single ``commit()`` so
    the value hook can observe the logical mutation before anything is
    persisted. This also fixes the historical ordering where the non-system
    "Name" path committed the new ObjectValue row *before* its own audit entry.
    CORESEAM-11/D-11 (291.2-04): the row also carries the mutation actor/origin
    so the later index hook names the initiator, not the worker.
    ``emit_index_outbox=False`` is the composite-writer mode (D-15) — the label
    still moves, but the caller owns the one object-level outbox row.
    Returns the outbox row for :func:`_finalize_object_name_sync` (``None`` when
    suppressed).
    """
    obj.name = new_name
    if not emit_index_outbox:
        return None
    outbox_entry = _new_outbox_entry(
        obj.id, obj.object_type_id, "index",
        actor_id=actor_id, origin=origin,
    )
    db.add(outbox_entry)
    return outbox_entry


async def _finalize_object_name_sync(
    db: AsyncSession, obj: Object, outbox_entry: IndexOutbox
) -> None:
    """Post-commit side effects of a staged name mirror (enqueue + cache)."""
    await _enqueue_index_job(db, obj.id, "index_object_job", outbox_id=outbox_entry.id)
    await invalidate_entity_cache("object")
    await invalidate_entity_cache("search")


async def _emit_value_change_hook(
    db: AsyncSession,
    *,
    obj: Object,
    attribute_id: uuid.UUID,
    old_rows: list[dict],
    new_rows: list[dict],
    old_obj_values: dict,
    actor_id: uuid.UUID,
    origin: hook_origin.MutationOrigin,
) -> None:
    """Emit the single owning-Object ``on_object_updated`` for a value change.

    D-14 payload contract: ``old_values``/``new_values`` are always Object column
    snapshots (``model_to_dict(obj)``), while the attribute change lives in
    ``changed_attribute_ids`` and ``attribute_changes``. The per-side rows are
    raw ``model_to_dict`` snapshots of the ObjectValue row (or a
    ``{"value_simple": <label>}`` dict for the valueless Name binding), so the
    value row id, OTA id and every typed column survive.
    """
    await _fire_object_hook(
        "on_object_updated",
        db,
        object_id=obj.id,
        schema_id=obj.schema_id,
        object_type_id=obj.object_type_id,
        old_values=old_obj_values,
        new_values=model_to_dict(obj),
        actor_id=actor_id,
        origin=origin.as_payload(),
        changed_attribute_ids=[attribute_id],
        attribute_changes=[
            {"attribute_id": attribute_id, "old": old_rows, "new": new_rows}
        ],
    )


def _resolve_select_display_in_place(items: list[ObjectValue]) -> list[ObjectValue]:
    """Resolve SELECT option ids on detached response copies before caching.

    Batched: per-attribute option maps are built once and reused across every
    row (never a structure walk per value). Non-SELECT attributes have no
    options -> map {} -> `resolve_display_value` is a no-op, so the pass is
    byte-identical for them (Test 5 contract). Unresolvable values (stale
    strings D-04, deleted-option uuids D-15) stay raw.
    """
    resolved_items = deepcopy(items)
    option_maps: dict[uuid.UUID, dict[str, str]] = {}
    for item in resolved_items:
        attr = item.object_type_attribute
        if attr is None:
            continue
        if attr.id not in option_maps:
            option_maps[attr.id] = build_option_map(attr)
        option_map = option_maps[attr.id]
        if option_map and item.value_simple:
            item.value_simple = resolve_display_value(item.value_simple, option_map)
    return resolved_items


async def _resolve_display_in_place_lang(
    db: AsyncSession, items: list[ObjectValue], lang: str
) -> list[ObjectValue]:
    """?lang= overlay on ORM ObjectValue rows BEFORE serialization/caching.

    V-01 chain, strictly interleaved by language level (requested language
    complete — both the per-object override and the option/status translation
    — before the schema default language is consulted at all):

      (a) per-object override in the requested language
      (b) option/status translation in the requested language
      (c) per-object override in the schema default language
      (d) option/status translation in the schema default language
      (e) base value

    ``resolve_translations`` reports the serving code per entity (requested
    OR schema-default, never anything else), so legs (a)/(c) and (b)/(d) are
    distinguished by ``serving == requested code``. The per-object override
    legs are V-05-gated inside (attr + parent OT multilingual + TEXT/SELECT/
    STATUS dtype); SELECT option translations are D-08/D-09-gated (owning
    attr + parent OT flags); STATUS name translations are D-04 envelope-only
    — never gated by the owning attribute's multilingual flag (gate-scope
    clarification 2026-08-10, 224-CONTEXT V-05). Ineligible rows yield no
    entries and keep their base values (dormant, 221 D-10).

    - TEXT: ``resolve_translations("object_value")`` overwrites value_simple.
    - SELECT: legs (a)-(d) overwrite value_simple; leg (e) resolves the
      stored option id to the base display / raw (D-15). ``item.value_simple``
      still holds the stored option id here — callers must NOT run
      ``_resolve_select_display_in_place`` first.
    - STATUS: legs (a)-(d) replace ``value_struct["name"]`` at read time;
      base struct keeps id + stored name.

    ``resolved_language`` is set ONLY when a translation actually served
    (D-19 byte-identical contract); base-served rows stay untouched.
    Batched only — one attr query + one resolve call per entity set, never
    a query per row.
    """
    if not items:
        return []
    items = deepcopy(items)
    attr_ids = {
        item.object_type_attribute_id
        for item in items
        if item.object_type_attribute_id is not None
    }
    attrs: dict[uuid.UUID, ObjectTypeAttribute] = {}
    if attr_ids:
        attr_result = await db.execute(
            select(ObjectTypeAttribute)
            .where(ObjectTypeAttribute.id.in_(attr_ids))
            .options(selectinload(ObjectTypeAttribute.attribute_type))
        )
        attrs = {a.id: a for a in attr_result.scalars().all()}

    eligible = [
        item for item in items
        if item.object_type_attribute_id in attrs
        and attrs[item.object_type_attribute_id].attribute_type is not None
        and attrs[item.object_type_attribute_id].attribute_type.data_type
        in (
            AttributeTypeDataType.TEXT.value,
            AttributeTypeDataType.SELECT.value,
            AttributeTypeDataType.STATUS.value,
        )
    ]
    if not eligible:
        return items

    # Per-object override rows — V-05-gated inside (attr + parent OT
    # multilingual + TEXT/SELECT/STATUS dtype).
    overrides = await resolve_translations(
        db, "object_value", [item.id for item in eligible], ["value"],
        lang, schema_id=None,
    )

    # SELECT option-translation rows — D-08/D-09-gated inside (owning attr +
    # parent OT multilingual). Option ids live in value_simple (post-223).
    select_rows = [
        item for item in eligible
        if attrs[item.object_type_attribute_id].attribute_type.data_type
        == AttributeTypeDataType.SELECT.value
        and item.value_simple
    ]
    option_ids: list[uuid.UUID] = []
    for item in select_rows:
        try:
            option_ids.append(uuid.UUID(item.value_simple))
        except (ValueError, TypeError, AttributeError):
            pass
    option_names = (
        await resolve_translations(
            db, "select_option", option_ids, ["value"], lang, schema_id=None,
        )
        if option_ids
        else {}
    )

    # STATUS name-translation rows — D-04 envelope-only, never V-05-gated
    # (gate-scope clarification 2026-08-10, 224-CONTEXT V-05).
    status_rows = [
        item for item in eligible
        if attrs[item.object_type_attribute_id].attribute_type.data_type
        == AttributeTypeDataType.STATUS.value
        and isinstance(item.value_struct, dict)
        and item.value_struct.get("id")
    ]
    status_ids: list[uuid.UUID] = []
    for item in status_rows:
        try:
            status_ids.append(uuid.UUID(str(item.value_struct.get("id"))))
        except (ValueError, TypeError, AttributeError):
            pass
    status_names = (
        await resolve_translations(
            db, "status", status_ids, ["name"], lang, schema_id=None,
        )
        if status_ids
        else {}
    )

    for item in eligible:
        attr = attrs[item.object_type_attribute_id]
        dtype = attr.attribute_type.data_type
        serving: str | None = None
        if dtype == AttributeTypeDataType.TEXT.value:
            entry = overrides.get(item.id, {}).get("value")
            if entry:
                item.value_simple = entry[0]
                serving = entry[1]
        elif dtype == AttributeTypeDataType.SELECT.value:
            entry = overrides.get(item.id, {}).get("value")
            opt_entry = None
            if item.value_simple:
                try:
                    opt_entry = option_names.get(
                        uuid.UUID(item.value_simple), {}
                    ).get("value")
                except (ValueError, TypeError, AttributeError):
                    pass
            if entry and entry[1] == lang:
                # (a) per-object override in the requested language.
                item.value_simple, serving = entry
            elif opt_entry and opt_entry[1] == lang:
                # (b) option translation in the requested language.
                item.value_simple, serving = opt_entry
            elif entry:
                # (c) per-object override in the schema default language.
                item.value_simple, serving = entry
            elif opt_entry:
                # (d) option translation in the schema default language.
                item.value_simple, serving = opt_entry
            else:
                # (e) base display (option map) / raw (D-15).
                item.value_simple = resolve_display_value(
                    item.value_simple, build_option_map(attr)
                )
        else:  # STATUS
            entry = overrides.get(item.id, {}).get("value")
            status_entry = None
            if isinstance(item.value_struct, dict) and item.value_struct.get("id"):
                try:
                    status_entry = status_names.get(
                        uuid.UUID(str(item.value_struct.get("id"))), {}
                    ).get("name")
                except (ValueError, TypeError, AttributeError):
                    pass
            if entry and entry[1] == lang and isinstance(item.value_struct, dict):
                # (a) per-object override in the requested language.
                item.value_struct["name"] = entry[0]
                serving = entry[1]
            elif status_entry and status_entry[1] == lang and isinstance(item.value_struct, dict):
                # (b) status name translation in the requested language.
                item.value_struct["name"] = status_entry[0]
                serving = status_entry[1]
            elif entry and isinstance(item.value_struct, dict):
                # (c) per-object override in the schema default language.
                item.value_struct["name"] = entry[0]
                serving = entry[1]
            elif status_entry and isinstance(item.value_struct, dict):
                # (d) status name translation in the schema default language.
                item.value_struct["name"] = status_entry[0]
                serving = status_entry[1]
            # (e) base struct keeps id + stored name.
        if serving is not None:
            item.resolved_language = serving
    return items


async def _validate_status_value(
    db: AsyncSession, attribute: ObjectTypeAttribute, value_struct: dict | None
) -> None:
    """Reject a Status value_struct that doesn't match a known local Status option.

    A Status attribute's valid values are exactly attribute.structure.status_options
    (populated by discover_statuses during Jira sync). Storing anything else — e.g.
    a Jira status id that was never locally discovered — would fabricate a Status
    that doesn't exist and can't be represented/selected in the UI.
    """
    if value_struct is None:
        return
    result = await db.execute(
        select(AttributeType).where(AttributeType.id == attribute.attribute_type_id)
    )
    attribute_type = result.scalar_one_or_none()
    if not attribute_type or attribute_type.data_type != AttributeTypeDataType.STATUS:
        return
    status_options = (attribute.structure or {}).get("status_options", [])
    valid_ids = {opt.get("id") for opt in status_options}
    if value_struct.get("id") not in valid_ids:
        raise ValueError(
            f"Status value {value_struct!r} does not match a known Status option "
            f"for attribute '{attribute.name}'"
        )


async def _validate_select_value(
    db: AsyncSession, attribute: ObjectTypeAttribute, value_simple: str | None
) -> str | None:
    """Resolve a SELECT display string to its canonical option id (D-05).

    SELECT is a value_simple-typed attribute; post-migration (223-01) the
    canonical stored value is the option's UUID id from the attribute's
    structure. Anti-fabrication (mirrors _validate_status_value): the stored
    value must be a known option id.

    Resolution order:
      1. value_simple parses as a UUID AND is in the option id set -> unchanged;
      2. value_simple equals an option's "value" -> that option's "id";
      3. else ValueError naming the attribute and the offending value (the
         caller converts it to a 400 — fabricated options are rejected).

    Returns None for non-SELECT attributes and None values (the caller stores
    the input unchanged).
    """
    if value_simple is None:
        return None
    result = await db.execute(
        select(AttributeType).where(AttributeType.id == attribute.attribute_type_id)
    )
    attribute_type = result.scalar_one_or_none()
    if not attribute_type or attribute_type.data_type != AttributeTypeDataType.SELECT:
        return None
    options = (attribute.structure or {}).get("options", [])
    option_ids = {str(opt.get("id")) for opt in options if opt.get("id")}
    try:
        parsed = uuid.UUID(value_simple)
    except (ValueError, TypeError):
        parsed = None
    if parsed is not None and str(parsed) in option_ids:
        return str(parsed)
    for opt in options:
        if opt.get("value") == value_simple and opt.get("id"):
            return str(opt["id"])
    raise ValueError(
        f"Select value {value_simple!r} does not match a known option "
        f"for attribute '{attribute.name}'"
    )


async def _validate_user_group_value(
    db: AsyncSession, attribute: ObjectTypeAttribute, value_simple: str | None
) -> None:
    """Validate a USER/GROUP value_simple against the users/groups tables (D-02/D-05).

    Early-returns when the attribute's data_type is neither USER nor GROUP, or
    when value_simple is None. A value_simple that fails to parse as a UUID, or
    that parses but doesn't exist in the correct table, raises ValueError with
    the same shape as _validate_status_value/_validate_select_value (the caller
    converts it to 400) — this also rejects a cross-table UUID (a GROUP UUID
    submitted for a USER attribute, or vice versa; D-03 contract).

    The GROUP branch lazy-imports the Group model (mirrors filter_utils' own
    local-import pattern for models introduced later in this same plan — Group
    lands in Task 3) so this function is safely callable from Task 1 onward.
    """
    if value_simple is None:
        return
    result = await db.execute(
        select(AttributeType).where(AttributeType.id == attribute.attribute_type_id)
    )
    attribute_type = result.scalar_one_or_none()
    if not attribute_type or attribute_type.data_type not in (
        AttributeTypeDataType.USER, AttributeTypeDataType.GROUP
    ):
        return

    # attribute_type.data_type comes back from SQLAlchemy as a plain str (the
    # column is String(50), not a native enum type) — str(...) is a no-op in
    # that case but also safely normalizes an actual enum member, so this
    # works regardless of which shape the ORM handed back.
    dtype_str = str(attribute_type.data_type)

    try:
        parsed = uuid.UUID(value_simple)
    except (ValueError, TypeError):
        raise ValueError(
            f"{dtype_str.capitalize()} value {value_simple!r} is not a "
            f"valid UUID for attribute '{attribute.name}'"
        )

    if attribute_type.data_type == AttributeTypeDataType.USER:
        from app.models.user import User
        lookup = await db.execute(select(User.id).where(User.id == parsed))
    else:
        from app.models.group import Group
        lookup = await db.execute(select(Group.id).where(Group.id == parsed))

    if lookup.scalar_one_or_none() is None:
        raise ValueError(
            f"{dtype_str.capitalize()} value {value_simple!r} does not "
            f"match a known {dtype_str} for attribute '{attribute.name}'"
        )


def _validate_and_sanitize_textarea_value_for_type(
    data_type: str | None, structure: dict | None, value_simple: str | None
) -> str | None:
    """Pure core of the data-type-specific value_simple write invariants.

    Shared by every ObjectValue writer so the API service and direct ORM
    writers (CSV import) enforce identical rules — WR-01 (276): CSV import
    constructed ObjectValue rows straight from the connector's coerced
    entries, bypassing the length caps and the Bleach write-time layer.

    Receives the already-resolved attribute type (data_type string) and the
    attribute's structure so callers that hold those in plain caches (CSV
    import's type_cache/attr_ref_cache) never need a DB round-trip or a
    possibly-expired ORM relationship access.

    Pydantic applies the loose 64,000-character ceiling before the API
    service runs. The attribute type is only available here, so this guard
    preserves the historical 5,000-character limit for every non-textarea
    value. HTML textarea values are allow-list sanitized here as the D-08
    write-time defense-in-depth layer; markdown and simpletext remain
    source-preserving.

    Returns the value that must be stored (the sanitized html textarea
    value), or None when the caller's value is stored unchanged.
    """
    if value_simple is None:
        return None
    if data_type is None or str(data_type) != AttributeTypeDataType.TEXTAREA.value:
        if len(value_simple) > 5000:
            raise ValueError(
                "value_simple must not exceed 5000 characters for non-textarea attributes"
            )
        return None
    if len(value_simple) > 64000:
        raise ValueError("value_simple must not exceed 64000 characters for textarea attributes")
    if (structure or {}).get("format", "simpletext") == "html":
        return sanitize_textarea_html(value_simple)
    return None


async def _validate_and_sanitize_textarea_value(
    db: AsyncSession, attribute: ObjectTypeAttribute, value_simple: str | None
) -> str | None:
    """Resolve the attribute's type, then apply the shared write invariants."""
    if value_simple is None:
        return None
    result = await db.execute(
        select(AttributeType).where(AttributeType.id == attribute.attribute_type_id)
    )
    attribute_type = result.scalar_one_or_none()
    return _validate_and_sanitize_textarea_value_for_type(
        str(attribute_type.data_type) if attribute_type else None,
        attribute.structure,
        value_simple,
    )


def _as_naive_utc(value):
    """Normalize a tz-aware datetime to the naive-UTC shape the DB stores."""
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _has_supplied_change(value: ObjectValue, supplied: dict) -> bool:
    """D-05: does the PATCH supply a field whose persisted value differs?

    Only fields present in ``model_fields_set`` with a non-None value are
    supplied; normalization/validation (SELECT id resolution, textarea
    sanitization) already ran, so the comparison is against the canonical
    persisted shape.
    """
    for field, new_value in supplied.items():
        if _as_naive_utc(getattr(value, field)) != _as_naive_utc(new_value):
            return True
    return False


async def create_object_value(
    db: AsyncSession,
    value_data: ObjectValueCreate,
    actor_id: uuid.UUID,
    *,
    origin: hook_origin.MutationOrigin | None = None,
    commit: bool = True,
    emit_index_outbox: bool = True,
    invalidate_cache: bool = True,
) -> ObjectValue:
    """Create one ObjectValue (D-15 composite switches — see create_object)."""
    attribute_result = await db.execute(
        select(ObjectTypeAttribute).where(
            ObjectTypeAttribute.id == value_data.object_type_attribute_id
        )
    )
    attribute = attribute_result.scalar_one_or_none()
    if not attribute:
        raise ValueError("Object type attribute not found")

    # 246 D-03/D-08/T-246-02: system-OTA write routing FIRST — the system
    # Key/Created/Updated OTAs are rejected regardless of the target object,
    # so the guard fires even for a nonexistent object. The system Name OTA
    # is a valueless binding to Object.name (D-04).
    if attribute.is_system and attribute.name in ("Key", "Created", "Updated"):
        if attribute.name == "Key":
            raise ValueError("Key is immutable and managed by the system.")
        raise ValueError("System timestamp attributes are managed by the system.")

    result = await db.execute(select(Object).where(Object.id == value_data.object_id))
    obj = result.scalar_one_or_none()
    if not obj:
        raise ValueError("Object not found")

    # 290 D-02/D-04: a real create on an externally managed value is denied
    # before any name sync, validation-driven mutation or insert. The system
    # Name binding only writes when a value is supplied — a valueless call is
    # a read-shaped no-op and stays allowed.
    name_binding_noop = (
        attribute.is_system and attribute.name == "Name" and not value_data.value_simple
    )
    if not name_binding_noop:
        await assert_value_writable(
            db,
            object_type_id=obj.object_type_id,
            attribute_id=attribute.id,
            origin=origin,
            object_id=obj.id,
        )

    effective_origin = _resolve_origin(origin, "object.value.create", actor_id)

    if attribute.is_system and attribute.name == "Name":
        # D-04: valueless binding — never stores an ObjectValue row (keeps
        # filter_utils "Name" special case valid); redirect to Object.name.
        if value_data.value_simple and value_data.value_simple != obj.name:
            # (b) a real label change emits exactly ONE owning-Object update
            # carrying the Name OTA id and the old/new label (review
            # correction 2026-09-27), before the first commit.
            old_label = obj.name
            old_obj_values = model_to_dict(obj)
            outbox_entry = _stage_object_name_sync(
                db, obj, value_data.value_simple,
                actor_id=actor_id, origin=effective_origin,
                emit_index_outbox=emit_index_outbox,
            )
            await _emit_value_change_hook(
                db,
                obj=obj,
                attribute_id=attribute.id,
                old_rows=[{"value_simple": old_label}],
                new_rows=[{"value_simple": value_data.value_simple}],
                old_obj_values=old_obj_values,
                actor_id=actor_id,
                origin=effective_origin,
            )
            if commit:
                await db.commit()
                if outbox_entry is not None:
                    await _finalize_object_name_sync(db, obj, outbox_entry)
            else:
                await db.flush()
        # (a) a valueless call or an unchanged label is a side-effect-free
        # no-op — no hook, no outbox row, no re-index (D-05).
        return _build_synthetic_system_value(obj, attribute, value_data.value_simple)

    await _validate_status_value(db, attribute, value_data.value_struct)

    # D-05: SELECT display strings resolve server-side to the option id BEFORE
    # the ObjectValue construction — otherwise the string, not the id, is
    # persisted (anti-fabrication: unknown options raise ValueError -> 400).
    resolved_select = await _validate_select_value(db, attribute, value_data.value_simple)
    if resolved_select is not None:
        value_data.value_simple = resolved_select

    # D-02/D-05: USER/GROUP values validate against the users/groups tables —
    # unknown or cross-table UUIDs raise ValueError -> 400 at the HTTP layer.
    await _validate_user_group_value(db, attribute, value_data.value_simple)

    sanitized_textarea = await _validate_and_sanitize_textarea_value(
        db, attribute, value_data.value_simple
    )
    if sanitized_textarea is not None:
        value_data.value_simple = sanitized_textarea

    if attribute.object_type_id != obj.object_type_id:
        # Check if the attribute's ObjectType is an ancestor of the object's ObjectType
        ancestor_check = text("""
            WITH RECURSIVE ancestors AS (
                SELECT id, parent_id, 0 AS depth FROM object_types WHERE id = :child_type_id
                UNION ALL
                SELECT ot.id, ot.parent_id, a.depth + 1
                FROM object_types ot INNER JOIN ancestors a ON ot.id = a.parent_id
                WHERE a.depth < 100
            )
            SELECT 1 FROM ancestors WHERE id = :attribute_type_id
        """)
        result = await db.execute(
            ancestor_check,
            {"child_type_id": obj.object_type_id, "attribute_type_id": attribute.object_type_id}
        )
        if result.scalar_one_or_none() is None:
            raise ValueError("Object type attribute does not belong to the object's ObjectType or its ancestors")

    db_value = ObjectValue(
        object_id=value_data.object_id,
        object_type_attribute_id=value_data.object_type_attribute_id,
        value_float=value_data.value_float,
        value_integer=value_data.value_integer,
        value_boolean=value_data.value_boolean,
        value_simple=value_data.value_simple,
        value_datetime=value_data.value_datetime,
        value_struct=value_data.value_struct,
    )
    db.add(db_value)

    # A non-system attribute literally named "Name" (e.g. a synced/manual
    # multilingual Name) is a NORMAL value change that additionally updates the
    # label — one hook, not two. Stage the mirror without committing so the hook
    # below observes the logical change before the single caller commit.
    old_obj_values = model_to_dict(obj)
    name_outbox_entry = None
    if attribute.name == "Name" and value_data.value_simple:
        name_outbox_entry = _stage_object_name_sync(
            db, obj, value_data.value_simple,
            actor_id=actor_id, origin=effective_origin,
            emit_index_outbox=emit_index_outbox,
        )

    await db.flush()
    new_values = resolve_select_option_display(
        model_to_dict(db_value), attribute.structure
    )
    await audit_log(db, "object_values", db_value.id, "CREATE", actor_id,
                    old_values=None, new_values=new_values, object_id=value_data.object_id,
                    object_type_attribute_id=value_data.object_type_attribute_id)
    # CORESEAM-11/D-04/D-05: one owning-Object update for a real value create,
    # in the writer's transaction and BEFORE the first commit. The snapshot is
    # the raw typed row (value row id, OTA id and typed columns preserved).
    await _emit_value_change_hook(
        db,
        obj=obj,
        attribute_id=value_data.object_type_attribute_id,
        old_rows=[],
        new_rows=[model_to_dict(db_value)],
        old_obj_values=old_obj_values,
        actor_id=actor_id,
        origin=effective_origin,
    )
    # D-15 (291.2-04): a real non-Name system-value mutation was an index
    # blindspot. Stage one attributed index row in the same transaction; a write
    # that also moved the label reuses the staged Name row instead.
    value_outbox_entry = None
    if emit_index_outbox and not attribute.is_system and name_outbox_entry is None:
        value_outbox_entry = _new_outbox_entry(
            obj.id, obj.object_type_id, "index",
            actor_id=actor_id, origin=effective_origin,
        )
        db.add(value_outbox_entry)
    if not commit:
        # Composite writer (D-15): the caller owns the single transaction,
        # the enqueue and the one cache purge. The "Updated" system value is
        # touched ONCE by that caller after the payload loop, never per value.
        await db.flush()
        return db_value
    await db.commit()
    if name_outbox_entry is not None:
        await _finalize_object_name_sync(db, obj, name_outbox_entry)
    if value_outbox_entry is not None:
        await _enqueue_index_job(db, obj.id, "index_object_job", outbox_id=value_outbox_entry.id)
    if attribute.name not in ("Created", "Updated"):
        await _touch_system_attribute(db, obj.id, obj.object_type_id, "Updated")
    await db.refresh(db_value)
    result = await db.execute(
        select(ObjectValue)
        .where(ObjectValue.id == db_value.id)
        .options(selectinload(ObjectValue.object_type_attribute))
    )
    loaded = result.scalar_one_or_none()
    if invalidate_cache:
        await invalidate_entity_cache(ENTITY_TYPE)
        await invalidate_entity_cache("search")
    return loaded or db_value


async def list_object_values(
    db: AsyncSession, skip: int = 0, limit: int = 100,
    exclude_hidden: bool = False,
    user_role: str | None = None,  # NEW per D-10
    lang: str | None = None,  # ?lang= resolution (224-03)
    *,
    user_id: str | None = None,
    is_superuser: bool = False,
    role_ids: list[uuid.UUID] | None = None,
    principal: str | None = None,
) -> tuple[List[ObjectValue], int]:
    # Security (F-88-03-05): exclude_hidden is folded into the cache key so superuser and
    # non-superuser results are never cross-served from the cache — a post-hoc Python filter
    # on already-cached results would leak hidden values on a cache hit.
    # 224-03: lang partitions the list cache per language (MULTI-06 pattern).
    # 317-03 (P-02): `principal` is the resolved role-set signature — the literal
    # user_role string is not a principal dimension, so two non-superusers with
    # different DENYs must never share one entry.
    cache_key = build_list_cache_key(ENTITY_TYPE, skip=skip, limit=limit, exclude_hidden=exclude_hidden, user_role=user_role, lang=lang, principal=principal)
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached["items"], cached["total"]

    query = select(ObjectValue)
    count_query = select(func.count()).select_from(ObjectValue)
    if not is_superuser and user_id is not None:
        # D-05: the predicate lives in the SQL, before anything is cached.
        conditions = await visible_value_conditions(
            db, user_id, False, role_ids=role_ids
        )
        # `Select.join()` takes one target (plus an optional onclause) per
        # call — the three join targets must be applied sequentially.
        for target, onclause in (
            (Object, Object.id == ObjectValue.object_id),
            (
                ObjectTypeAttribute,
                ObjectTypeAttribute.id == ObjectValue.object_type_attribute_id,
            ),
            (
                AttributeType,
                AttributeType.id == ObjectTypeAttribute.attribute_type_id,
            ),
        ):
            query = query.join(target, onclause)
            count_query = count_query.join(target, onclause)
        query = query.where(*conditions)
        count_query = count_query.where(*conditions)
    elif exclude_hidden:
        query = query.join(ObjectTypeAttribute).where(ObjectTypeAttribute.is_hidden.is_(False))
        count_query = count_query.join(ObjectTypeAttribute).where(ObjectTypeAttribute.is_hidden.is_(False))

    count_result = await db.execute(count_query)
    total = count_result.scalar()
    result = await db.execute(
        query.options(selectinload(ObjectValue.object_type_attribute))
        .offset(skip).limit(limit)
    )
    items = result.scalars().all()

    # T-223-18: resolve SELECT ids BEFORE caching — a first-request cache MISS
    # must never populate the cache with unresolved ORM values (221/222
    # cold-cache pattern). Cached payloads are serialized via the response
    # model (AGENTS.md hard rule — includes the attribute_name property).
    # 224-03: with ?lang= the V-01 overlay runs instead (it performs the base
    # SELECT pass itself); resolution happens BEFORE the cache set either way.
    if lang:
        items = await _resolve_display_in_place_lang(db, list(items), lang)
    else:
        items = _resolve_select_display_in_place(list(items))
    await set_cached_with_tags(
        cache_key,
        {
            "items": [
                ObjectValueResponse.model_validate(item).model_dump(mode="json")
                for item in items
            ],
            "total": total,
        },
        tags=[build_tag_key(ENTITY_TYPE)],
        ttl=CACHE_TTL_LIST,
    )
    return items, total


async def get_object_value(
    db: AsyncSession, value_id: uuid.UUID, lang: str | None = None
) -> ObjectValue | dict | None:
    # 224-03: lang-scoped entity key (221 MULTI-06 pattern) — a ?lang=fr
    # request can never serve a cached ?lang=en payload.
    cache_key = build_cache_key(
        ENTITY_TYPE, f"{value_id}:{lang}" if lang else str(value_id)
    )
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached
    result = await db.execute(
        select(ObjectValue)
        .options(selectinload(ObjectValue.object_type_attribute))
        .where(ObjectValue.id == value_id)
    )
    value = result.scalar_one_or_none()
    if value:
        # T-223-18: resolve BEFORE caching (cold-cache correctness); cached
        # payload serialized via the response model so the attribute_name
        # property survives (AGENTS.md hard rule). The nested
        # object_type_attribute is preserved for the route's hidden-attribute
        # check (F-88-03-05) — the response model itself doesn't carry it.
        if lang:
            value = (await _resolve_display_in_place_lang(db, [value], lang))[0]
        else:
            value = _resolve_select_display_in_place([value])[0]
        payload = ObjectValueResponse.model_validate(value).model_dump(mode="json")
        if value.object_type_attribute:
            payload["object_type_attribute"] = jsonable_encoder(
                value.object_type_attribute
            )
        await set_cached_with_tags(
            cache_key,
            payload,
            tags=[build_tag_key(ENTITY_TYPE)],
            ttl=CACHE_TTL_ENTITY,
        )
    return value


async def update_object_value(
    db: AsyncSession,
    value_id: uuid.UUID,
    value_update: ObjectValueUpdate,
    actor_id: uuid.UUID,
    *,
    origin: hook_origin.MutationOrigin | None = None,
    commit: bool = True,
    emit_index_outbox: bool = True,
    invalidate_cache: bool = True,
) -> ObjectValue | None:
    """Update one ObjectValue (D-15 composite switches — see create_object)."""
    result = await db.execute(
        select(ObjectValue)
        .options(selectinload(ObjectValue.object_type_attribute))
        .where(ObjectValue.id == value_id)
    )
    value = result.scalar_one_or_none()
    if not value:
        return None

    value_attr = value.object_type_attribute if value.object_type_attribute else None
    if value_attr and value_attr.is_system:
        # 246 D-03/D-08/T-246-02: system-OTA write routing on the update path.
        if value_attr.name == "Key":
            raise ValueError("Key is immutable and managed by the system.")
        if value_attr.name in ("Created", "Updated"):
            raise ValueError("System timestamp attributes are managed by the system.")

    if value_update.value_struct is not None and value.object_type_attribute:
        await _validate_status_value(db, value.object_type_attribute, value_update.value_struct)

    # D-05: same SELECT resolution on the update path — resolve BEFORE the
    # assignment below so the resolved id (not the raw display string) is stored.
    if value_update.value_simple is not None and value.object_type_attribute:
        resolved_select = await _validate_select_value(
            db, value.object_type_attribute, value_update.value_simple
        )
        if resolved_select is not None:
            value_update.value_simple = resolved_select

    # D-02/D-05: same USER/GROUP validation on the update path.
    if value_update.value_simple is not None and value.object_type_attribute:
        await _validate_user_group_value(db, value.object_type_attribute, value_update.value_simple)

        sanitized_textarea = await _validate_and_sanitize_textarea_value(
            db, value.object_type_attribute, value_update.value_simple
        )
        if sanitized_textarea is not None:
            value_update.value_simple = sanitized_textarea

    # 290 D-05: an unchanged PATCH is a side-effect-free no-op — checked before
    # the lock (a locked value must still accept an identical payload) and
    # before the audit/name-sync/commit path. The system Name row keeps its
    # in-place update path below (253 D-09).
    supplied = {
        field: getattr(value_update, field)
        for field in value_update.model_fields_set
        if getattr(value_update, field) is not None
    }
    if not _has_supplied_change(value, supplied):
        return value

    # 290 D-02/D-04: a real change to an externally managed value is denied
    # before audit/hooks/cache/commit. Authority is the fresh persisted marker
    # (never the metadata cache).
    obj_result = await db.execute(select(Object).where(Object.id == value.object_id))
    obj = obj_result.scalar_one_or_none()
    if obj is not None:
        await assert_value_writable(
            db,
            object_type_id=obj.object_type_id,
            attribute_id=value.object_type_attribute_id,
            origin=origin,
            object_id=value.object_id,
        )

    effective_origin = _resolve_origin(origin, "object.value.update", actor_id)
    old_obj_values = model_to_dict(obj) if obj is not None else None
    old_values = model_to_dict(value)
    old_simple = value.value_simple
    old_struct = value.value_struct
    if value_update.value_float is not None:
        value.value_float = value_update.value_float
    if value_update.value_integer is not None:
        value.value_integer = value_update.value_integer
    if value_update.value_boolean is not None:
        value.value_boolean = value_update.value_boolean
    if value_update.value_simple is not None:
        value.value_simple = value_update.value_simple
    if value_update.value_datetime is not None:
        value.value_datetime = value_update.value_datetime
    if value_update.value_struct is not None:
        value.value_struct = value_update.value_struct

    # V-09: purge translations when the stored base value actually changes.
    # Must run BEFORE _sync_object_name's internal commit so the purge stays in
    # the same transaction as the base-value write.
    if value.value_simple != old_simple or value.value_struct != old_struct:
        await delete_translations_for(db, "object_value", [value_id])
        if invalidate_cache:
            await invalidate_entity_cache("object")

    new_values = model_to_dict(value)
    audit_structure = (
        value.object_type_attribute.structure if value.object_type_attribute else None
    )
    await audit_log(db, "object_values", value_id, "UPDATE", actor_id,
                    old_values=resolve_select_option_display(old_values, audit_structure),
                    new_values=resolve_select_option_display(new_values, audit_structure),
                    object_id=value.object_id,
                    object_type_attribute_id=value.object_type_attribute_id)

    # A non-system "Name" attribute plus a supplied value also updates the label
    # — still ONE hook. Stage the mirror without committing so the hook below
    # observes the logical change before the caller's single commit.
    name_outbox_entry = None
    if (
        value.object_type_attribute
        and value.object_type_attribute.name == "Name"
        and value_update.value_simple
        and obj is not None
    ):
        name_outbox_entry = _stage_object_name_sync(
            db, obj, value_update.value_simple,
            actor_id=actor_id, origin=effective_origin,
            emit_index_outbox=emit_index_outbox,
        )

    # CORESEAM-11/D-04/D-05: one owning-Object update for a real value change,
    # after validation/lock/audit and BEFORE the first commit. Object snapshots
    # are Object columns; the typed value row lives in attribute_changes.
    if obj is not None:
        await _emit_value_change_hook(
            db,
            obj=obj,
            attribute_id=value.object_type_attribute_id,
            old_rows=[old_values],
            new_rows=[new_values],
            old_obj_values=old_obj_values,
            actor_id=actor_id,
            origin=effective_origin,
        )

    # D-15 (291.2-04): a real non-Name system-value change must re-index too.
    # One attributed row in the same transaction; a label move reuses the
    # staged Name row so a single write never enqueues twice.
    value_outbox_entry = None
    if (
        emit_index_outbox
        and obj is not None
        and value.object_type_attribute is not None
        and not value.object_type_attribute.is_system
        and name_outbox_entry is None
    ):
        value_outbox_entry = _new_outbox_entry(
            obj.id, obj.object_type_id, "index",
            actor_id=actor_id, origin=effective_origin,
        )
        db.add(value_outbox_entry)

    if not commit:
        # Composite writer (D-15): one transaction, one enqueue and one cache
        # purge belong to the caller; the Updated touch happens once there.
        await db.flush()
        return value
    await db.commit()
    if name_outbox_entry is not None:
        await _finalize_object_name_sync(db, obj, name_outbox_entry)
    if value_outbox_entry is not None:
        await _enqueue_index_job(db, obj.id, "index_object_job", outbox_id=value_outbox_entry.id)
    attr_name = value.object_type_attribute.name if value.object_type_attribute else None
    if attr_name not in ("Created", "Updated"):
        if obj is None:
            obj_result = await db.execute(select(Object).where(Object.id == value.object_id))
            obj = obj_result.scalar_one_or_none()
        if obj:
            await _touch_system_attribute(db, obj.id, obj.object_type_id, "Updated")
    await db.refresh(value)
    result = await db.execute(
        select(ObjectValue)
        .where(ObjectValue.id == value.id)
        .options(selectinload(ObjectValue.object_type_attribute))
    )
    value = result.scalar_one_or_none()
    if invalidate_cache:
        await invalidate_entity_cache(ENTITY_TYPE)
        await invalidate_entity_cache("search")
    return value


async def delete_object_value(
    db: AsyncSession,
    value_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    origin: hook_origin.MutationOrigin | None = None,
    commit: bool = True,
    emit_index_outbox: bool = True,
    invalidate_cache: bool = True,
) -> bool:
    """Delete one ObjectValue (D-15 composite switches — see create_object)."""
    result = await db.execute(
        select(ObjectValue)
        .options(selectinload(ObjectValue.object_type_attribute))
        .where(ObjectValue.id == value_id)
    )
    value = result.scalar_one_or_none()
    if not value:
        return False
    object_id = value.object_id
    attr_name = value.object_type_attribute.name if value.object_type_attribute else None
    # 290 D-02/D-04: a real deletion of an externally managed value is denied
    # before the audit/translation purge/commit. Authority is the fresh
    # persisted marker (never the metadata cache).
    obj_result = await db.execute(select(Object).where(Object.id == object_id))
    obj = obj_result.scalar_one_or_none()
    if obj is not None:
        await assert_value_writable(
            db,
            object_type_id=obj.object_type_id,
            attribute_id=value.object_type_attribute_id,
            origin=origin,
            object_id=object_id,
        )
    # 253 D-09: the persisted system Name row mirrors Object.name — deleting it
    # would break the binding the create path establishes (D-01). The detail
    # panel reaches this route by clearing the cell, so the guard has to live
    # here, not only in the UI. Key/Created/Updated already reject writes in
    # update_object_value; Name gets the same treatment on the delete path.
    if value.object_type_attribute is not None and value.object_type_attribute.is_system and attr_name == "Name":
        raise ValueError(
            "System Name attribute is managed by the system — rename the object via /objects/{id} instead."
        )
    effective_origin = _resolve_origin(origin, "object.value.delete", actor_id)
    old_obj_values = model_to_dict(obj) if obj is not None else None
    old_values = model_to_dict(value)
    audit_structure = (
        value.object_type_attribute.structure if value.object_type_attribute else None
    )
    await audit_log(db, "object_values", value_id, "DELETE", actor_id,
                    old_values=resolve_select_option_display(old_values, audit_structure),
                    new_values=None, object_id=object_id,
                    object_type_attribute_id=value.object_type_attribute_id)
    # V-11: purge translations in the same transaction as the value deletion.
    await delete_translations_for(db, "object_value", [value_id])
    if invalidate_cache:
        await invalidate_entity_cache("object")
    # CORESEAM-11/D-04/D-06: fire the owning-Object update after lock/audit but
    # BEFORE db.delete(value), with the raw typed snapshot captured above and an
    # explicit empty new side. Best-effort before the first commit (D-05).
    if obj is not None:
        await _emit_value_change_hook(
            db,
            obj=obj,
            attribute_id=value.object_type_attribute_id,
            old_rows=[old_values],
            new_rows=[],
            old_obj_values=old_obj_values,
            actor_id=actor_id,
            origin=effective_origin,
        )
    # D-15 (291.2-04): a real non-Name value delete must drop the value from the
    # index too. One attributed row in the same transaction; system attributes
    # stay out of this producer.
    value_outbox_entry = None
    if (
        emit_index_outbox
        and value.object_type_attribute is not None
        and not value.object_type_attribute.is_system
    ):
        value_outbox_entry = _new_outbox_entry(
            object_id, obj.object_type_id if obj is not None else None, "index",
            actor_id=actor_id, origin=effective_origin,
        )
        db.add(value_outbox_entry)
    await db.delete(value)
    if not commit:
        # Composite writer (D-15): see update_object_value.
        await db.flush()
        return True
    await db.commit()
    if value_outbox_entry is not None:
        await _enqueue_index_job(db, object_id, "index_object_job", outbox_id=value_outbox_entry.id)
    if obj and attr_name not in ("Created", "Updated"):
        await _touch_system_attribute(db, obj.id, obj.object_type_id, "Updated")
    if invalidate_cache:
        await invalidate_entity_cache(ENTITY_TYPE)
        await invalidate_entity_cache("search")
    return True
