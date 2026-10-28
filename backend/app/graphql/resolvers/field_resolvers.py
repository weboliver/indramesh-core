"""Field resolver functions for Strawberry GraphQL types.

Each resolver is an async function taking ``(root, info)`` where ``root`` is
the parent GraphQL type instance and ``info`` carries the DataLoader context
(info.context.loaders).  All nested-field resolution goes through DataLoaders
to prevent N+1 queries (D-179-05, T-182-07).

CRITICAL: This module MUST NOT import from ``app.graphql.types.*`` at module
level — doing so recreates the type → field_resolvers → types cycle that the
lazy-import pattern in mappers.py exists to break.  Return types are either
omitted or expressed as string annotations under TYPE_CHECKING guards, and the
runtime construction helpers are imported inside the function that needs them.
There is no exception to this rule: importing ``app.graphql.types.classification``
also executes the ``app.graphql.types`` package ``__init__``, which eagerly
imports ``object_type`` → ``schema`` → this module, so even that type module
closes the cycle.

Record-level read DENY (quick 261011-tv1): downward and sideways single
fields (ObjectRef.fromObject/toObject/objectType/source, ObjectType.parent)
return null for a DENY'd record, and list fields (Object.referencesFrom/
referencesTo, ObjectType.children, Schema.objectTypes) are filtered, both via
app.graphql.record_visibility.  Upward fields are unchecked by design (deny
wins across the record chain).  ObjectValue.attribute nulls its defining-type
fields for a DENY'd defining type (quick 261011-wfr), and inheritedAttributes
is filtered by its loader.  Fields without a hierarchy record model (icon,
user/group names) are unchanged.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from fastapi import HTTPException
from graphql import GraphQLError
from strawberry.types import Info

from app.config import settings
from app.graphql.record_visibility import record_visible, visible_ids
from app.graphql.resolvers import mappers
from app.services.select_option_service import (
    build_option_map,
    resolve_display_value,
    resolve_select_values,
)

if TYPE_CHECKING:
    from typing import Optional

    from app.graphql.context import GraphQLContext
    from app.graphql.types.classification import ClassificationLabel


# ---------------------------------------------------------------------------
# Classification markers (D-07, CLASS-02/CLASS-04/CLASS-05)
# ---------------------------------------------------------------------------

FAMILY_CONFIDENTIALITY = "confidentiality"
FAMILY_PERSONAL_DATA = "personal_data"
FAMILY_EXPORT_CONTROL = "export_control"


async def _classification_refs(root, info: Info, loader_name: str) -> list[dict]:
    """Load this parent's label refs through the request-scoped DataLoader.

    LOCK-FREE on purpose: the loader's ``_load_fn`` takes ``info.context.lock``
    around the shared-session dispatch (T-317.1-16). The loader caches per key,
    so the four classification fields of ONE parent cost a single batch.
    """
    loader = getattr(info.context.loaders, loader_name)
    return await loader.load(root.id) or []


def _label_from_ref(ref: dict) -> ClassificationLabel:
    """Map one loader ref to the GraphQL type (lazy import: see module docstring)."""
    from app.graphql.types.classification import classification_label_from_ref

    return classification_label_from_ref(ref)


def _labels_of_family(refs: list[dict], family: str) -> list[ClassificationLabel]:
    return [_label_from_ref(ref) for ref in refs if ref["family"] == family]


async def resolve_attribute_classification_labels(root, info: Info):
    """Resolve ``ObjectTypeAttribute.classificationLabels`` (D-07).

    ``root.id`` is the DEFINING attribute row, also for an inherited attribute
    reached through ``inheritedAttributes`` — so the child reports its defining
    row's labels (D-06). An unclassified attribute answers ``[]``.
    """
    refs = await _classification_refs(root, info, "classification_labels_by_attribute")
    return [_label_from_ref(ref) for ref in refs]


async def resolve_attribute_confidentiality(root, info: Info):
    """Resolve ``ObjectTypeAttribute.confidentiality`` — the one family label or null."""
    refs = await _classification_refs(root, info, "classification_labels_by_attribute")
    labels = _labels_of_family(refs, FAMILY_CONFIDENTIALITY)
    return labels[0] if labels else None


async def resolve_attribute_personal_data(root, info: Info):
    """Resolve ``ObjectTypeAttribute.personalData`` — family presence, never a column."""
    refs = await _classification_refs(root, info, "classification_labels_by_attribute")
    return bool(_labels_of_family(refs, FAMILY_PERSONAL_DATA))


async def resolve_attribute_export_control(root, info: Info):
    """Resolve ``ObjectTypeAttribute.exportControl`` — the family's labels (CLASS-04)."""
    refs = await _classification_refs(root, info, "classification_labels_by_attribute")
    return _labels_of_family(refs, FAMILY_EXPORT_CONTROL)


async def resolve_object_classification_labels(root, info: Info):
    """Resolve ``Object.classificationLabels`` (D-07, CLASS-05).

    Runs only for an object the caller already received — every root and
    relationship path filters visibility first, so no label of an invisible
    record is reachable (T-317.1-14).
    """
    refs = await _classification_refs(root, info, "classification_labels_by_object")
    return [_label_from_ref(ref) for ref in refs]


async def resolve_object_confidentiality(root, info: Info):
    """Resolve ``Object.confidentiality`` — the one family label or null."""
    refs = await _classification_refs(root, info, "classification_labels_by_object")
    labels = _labels_of_family(refs, FAMILY_CONFIDENTIALITY)
    return labels[0] if labels else None


async def resolve_object_personal_data(root, info: Info):
    """Resolve ``Object.personalData`` — family presence, never a column."""
    refs = await _classification_refs(root, info, "classification_labels_by_object")
    return bool(_labels_of_family(refs, FAMILY_PERSONAL_DATA))


async def resolve_object_export_control(root, info: Info):
    """Resolve ``Object.exportControl`` — the family's labels (CLASS-04)."""
    refs = await _classification_refs(root, info, "classification_labels_by_object")
    return _labels_of_family(refs, FAMILY_EXPORT_CONTROL)



# ---------------------------------------------------------------------------
# Single-parent resolvers (Pattern A: Private FK → by-id DataLoader → mapper)
# ---------------------------------------------------------------------------


async def resolve_object_schema(root, info: Info):
    """Resolve Object.schema from root._schema_id via the schemas DataLoader.

    Upward link on the record chain: a visible child implies no DENY here,
    because deny wins across the chain (quick 261011-tv1).
    """
    orm_schema = await info.context.loaders.schemas.load(root._schema_id)
    if orm_schema is None:
        return None
    return mappers.schema_from_orm(orm_schema)


async def resolve_object_object_type(root, info: Info):
    """Resolve Object.object_type from root._object_type_id via the object_types
    DataLoader.

    Upward link on the record chain: a visible child implies no DENY here,
    because deny wins across the chain (quick 261011-tv1).
    """
    orm_ot = await info.context.loaders.object_types.load(root._object_type_id)
    if orm_ot is None:
        return None
    return mappers.object_type_from_orm(orm_ot)


async def resolve_object_type_schema(root, info: Info):
    """Resolve ObjectType.schema from root._schema_id.

    Upward link on the record chain: a visible child implies no DENY here,
    because deny wins across the chain (quick 261011-tv1).
    """
    orm_schema = await info.context.loaders.schemas.load(root._schema_id)
    if orm_schema is None:
        return None
    return mappers.schema_from_orm(orm_schema)


async def resolve_schema_source(root, info: Info):
    """Resolve Schema.source from root._source_id.

    Upward link on the record chain: a visible child implies no DENY here,
    because deny wins across the chain (quick 261011-tv1).
    """
    orm_source = await info.context.loaders.sources.load(root._source_id)
    if orm_source is None:
        return None
    return mappers.source_from_orm(orm_source)


async def resolve_object_ref_object_type(root, info: Info):
    """Resolve ObjectRef.object_type from root._object_type_id.

    Null on record-level DENY: the id is supplied by the client at ref
    creation and is not on the endpoint's chain (quick 261011-tv1).
    """
    orm_ot = await info.context.loaders.object_types.load(root._object_type_id)
    if orm_ot is None:
        return None
    if not await record_visible(info, "object_type", root._object_type_id):
        return None
    return mappers.object_type_from_orm(orm_ot)


async def resolve_object_ref_source(root, info: Info):
    """Resolve ObjectRef.source from root._source_id.

    Null on record-level DENY: the id is supplied by the client at ref
    creation and is not on the endpoint's chain (quick 261011-tv1).
    """
    orm_source = await info.context.loaders.sources.load(root._source_id)
    if orm_source is None:
        return None
    if not await record_visible(info, "source", root._source_id):
        return None
    return mappers.source_from_orm(orm_source)


# ---------------------------------------------------------------------------
# SELECT value / status resolution at the data-loading layer (223-05 D-13)
# ---------------------------------------------------------------------------
#
# mappers.object_value_from_orm stays a PURE mapper — the SELECT value
# resolution happens HERE, right after the batched loader fetch and BEFORE
# mapping, where DB access + the per-request context (session, lock, lang)
# are available. The OTA structures needed for the option maps are batch-
# loaded through the object_type_attributes DataLoader (one loader batch,
# never a query per value).


async def _load_value_otas(info: Info, orm_values: list) -> list:
    """Batch-load the ObjectTypeAttribute rows for a value set."""
    ota_ids = list(
        {v.object_type_attribute_id for v in orm_values if v.object_type_attribute_id}
    )
    if not ota_ids:
        return []
    loaded = await info.context.loaders.object_type_attributes.load_many(ota_ids)
    return [ota for ota in loaded if ota is not None]


async def _resolve_select_base(info: Info, orm_values: list) -> None:
    """Base SELECT resolution (no lang): option id -> display value.

    Per-attribute option maps are built once per value set and reused across
    every row; non-SELECT attributes yield {} -> byte-identical pass-through;
    unresolvable values stay raw (D-15). In-place on the ORM rows so the
    mapper sees pre-resolved values.
    """
    otas = await _load_value_otas(info, orm_values)
    option_maps: dict[uuid.UUID, dict[str, str]] = {
        ota.id: build_option_map(ota) for ota in otas
    }
    for v in orm_values:
        option_map = option_maps.get(v.object_type_attribute_id)
        if option_map and v.value_simple:
            v.value_simple = resolve_display_value(v.value_simple, option_map)


async def _resolve_select_values_lang(info: Info, root, orm_values: list, lang: str) -> None:
    """Lang overlay for SELECT values (X-Lang header, 223-05 D-13).

    Strict per-object schema scope: the object's schema is known, so an
    unknown/inactive code AND a code not enabled for that schema surface as
    the 400-convention GraphQL error (D-05/D-06, same messages as REST).
    The resolution itself is batched per value set with the full chain
    translated -> default -> base -> raw (D-19/D-15).
    """
    from app.services.translation_service import validate_read_language

    schema_id = getattr(root, "_schema_id", None)
    try:
        async with info.context.lock:
            await validate_read_language(info.context.session, lang, schema_id)
    except HTTPException as exc:
        raise GraphQLError(str(exc)) from exc

    otas = await _load_value_otas(info, orm_values)
    stored_by_attr: dict[uuid.UUID, list] = {}
    for v in orm_values:
        stored_by_attr.setdefault(v.object_type_attribute_id, []).append(v.value_simple)
    async with info.context.lock:
        resolved = await resolve_select_values(
            info.context.session, otas, stored_by_attr, lang
        )
    consumed: dict[uuid.UUID, int] = {aid: 0 for aid in resolved}
    for v in orm_values:
        displays = resolved.get(v.object_type_attribute_id)
        if displays:
            v.value_simple = displays[consumed[v.object_type_attribute_id]]
            consumed[v.object_type_attribute_id] += 1


# ---------------------------------------------------------------------------
# List / relationship resolvers
# ---------------------------------------------------------------------------


def cap_list(info: Info, field_name: str, items: list) -> list:
    """Cap one parent's list field at ``GRAPHQL_NESTED_LIST_LIMIT`` (D-10/D-19).

    The cap bounds the RESPONSE, not the DB read: the relationship
    DataLoaders still materialise every row of a parent before this runs (the
    cost rule bounds how many parents a document may request; pushing a
    per-parent LIMIT into the loader SQL is a post-1.0 optimisation, not this
    phase).

    The truncation is reported ONCE PER FIELD NAME per request: the first
    truncation of ``field_name`` appends a ``LIST_TRUNCATED`` GraphQLError to
    ``info.context.soft_errors`` — surfaced by ``SoftErrorExtension`` with
    ``data`` intact — and memoises it on ``info.context.truncated_fields``;
    later parents truncated on the same field add nothing. A client can
    therefore never mistake a truncated list for a complete one, and a
    request over many over-cap parents still yields exactly one entry
    (D-19: never a silent cut, never a rejected legitimate query).

    Callers pass the list AFTER record-level visibility filtering, so a
    DENY'd row neither appears in the truncated window nor consumes one of
    the cap's slots (T-312-09).

    The value is read at call time — never captured at import — so tests can
    monkeypatch ``settings.graphql_nested_list_limit``.
    """
    cap = settings.graphql_nested_list_limit
    if len(items) <= cap:
        return items
    reported = info.context.truncated_fields
    if field_name not in reported:
        error = GraphQLError(
            f"{field_name} was truncated to {cap} entries",
            extensions={"code": "LIST_TRUNCATED", "field": field_name, "cap": cap},
        )
        reported[field_name] = error
        info.context.soft_errors.append(error)
    return items[:cap]


async def resolve_object_values(root, info: Info):
    """Resolve Object.values via the object_values_by_object grouped DataLoader.

    SELECT option ids resolve to display values at this layer (223-05 D-13):
    always to base values (post-migration byte-identical contract), and with
    the context's ``lang`` (X-Lang header) through the translation overlay.
    Status values keep their raw value_struct — no status-name field exists
    on the GraphQL ObjectValue surface (documented in the 223-05 summary).

    The per-parent hard cap (GQLC-03/D-10) is applied to the ORM list after
    the language/select resolution and before mapping, so at most
    ``GRAPHQL_NESTED_LIST_LIMIT`` values come back for one object and the
    truncation is reported through ``errors[]`` (D-19).
    """
    orm_values = await info.context.loaders.object_values_by_object.load(root.id)
    orm_values = orm_values or []
    if orm_values:
        lang = getattr(info.context, "lang", None)
        if lang:
            await _resolve_select_values_lang(info, root, orm_values, lang)
        else:
            await _resolve_select_base(info, orm_values)
    orm_values = cap_list(info, "values", orm_values)
    return [mappers.object_value_from_orm(v) for v in orm_values]


async def resolve_object_value_attribute(root, info: Info):
    """Resolve ObjectValue.attribute via the object_type_attributes by-id DataLoader.

    The ObjectValue ORM row carries only object_type_attribute_id; the full
    definition (name, data_type, constraints) is resolved here.  data_type
    lives on AttributeType (not ObjectTypeAttribute), so it is batch-loaded
    via the attribute_types DataLoader — same pattern as
    resolve_inherited_attributes.  From a value's perspective the referenced
    attribute is never inherited: the value points at the defining OTA row,
    so is_inherited=False and defining_object_type_id is the OTA's own type.
    The defining type is not on the value's record chain, so a DENY'd defining
    type yields null id/name, while the definition itself stays readable for
    the visible object's value (quick 261011-wfr).
    """
    if root._object_type_attribute_id is None:
        return None
    orm_ota = await info.context.loaders.object_type_attributes.load(
        root._object_type_attribute_id
    )
    if orm_ota is None:
        return None

    at_row = None
    if orm_ota.attribute_type_id is not None:
        at_row = await info.context.loaders.attribute_types.load(
            orm_ota.attribute_type_id
        )
    # data_type may be a string (ORM column value) or an enum member.
    # Handle both so `data_type.value` doesn't crash on plain strings.
    if at_row is not None:
        raw = getattr(at_row, "data_type", "text")
        db_data_type = raw.value if hasattr(raw, "value") else raw
    else:
        db_data_type = "text"
    if hasattr(db_data_type, "value"):
        db_data_type = db_data_type.value

    defining_id = None
    defining_name = None
    if await record_visible(info, "object_type", orm_ota.object_type_id):
        defining_id = orm_ota.object_type_id
        orm_def_ot = await info.context.loaders.object_types.load(
            orm_ota.object_type_id
        )
        if orm_def_ot is not None:
            defining_name = orm_def_ot.name

    row = {c.name: getattr(orm_ota, c.name) for c in orm_ota.__table__.columns}
    row["defining_object_type_id"] = defining_id
    row["defining_object_type_name"] = defining_name
    row["is_inherited"] = False

    return mappers.object_type_attribute_from_orm(row, db_data_type)


async def _resolve_object_value_data_type(root, info: Info) -> str:
    """Resolve ObjectValue's own attribute data_type string (D-17 helper).

    Shares the same DataLoader chain as resolve_object_value_attribute
    (object_type_attributes -> attribute_types) but returns the raw
    lower-case data_type string instead of a full mapped GraphQL type —
    used by resolve_object_value_user_name/group_name to gate resolution to
    exactly the matching data_type. Defaults to "text" (never resolved) on
    any missing link, mirroring resolve_object_value_attribute's fallback.
    """
    if root._object_type_attribute_id is None:
        return "text"
    orm_ota = await info.context.loaders.object_type_attributes.load(
        root._object_type_attribute_id
    )
    if orm_ota is None or orm_ota.attribute_type_id is None:
        return "text"
    at_row = await info.context.loaders.attribute_types.load(orm_ota.attribute_type_id)
    if at_row is None:
        return "text"
    raw = getattr(at_row, "data_type", "text")
    return raw.value if hasattr(raw, "value") else raw


async def resolve_object_value_user_name(root, info: Info) -> Optional[str]:
    """Resolve ObjectValue.user_name (D-17): value_simple parsed as a UUID,
    resolved to the user's username via the users by-id DataLoader — only
    for a USER-typed attribute. Null for any other data_type, a non-UUID
    value, or a UUID absent from the users table (never fabricated)."""
    if not root.value_simple:
        return None
    try:
        user_id = uuid.UUID(root.value_simple)
    except (ValueError, TypeError, AttributeError):
        return None
    data_type = await _resolve_object_value_data_type(root, info)
    if data_type != "user":
        return None
    orm_user = await info.context.loaders.users.load(user_id)
    if orm_user is None:
        return None
    return orm_user.username


async def resolve_object_value_group_name(root, info: Info) -> Optional[str]:
    """Resolve ObjectValue.group_name (D-17) — same contract as
    resolve_object_value_user_name, gated on data_type == "group" and
    resolved via the groups by-id DataLoader."""
    if not root.value_simple:
        return None
    try:
        group_id = uuid.UUID(root.value_simple)
    except (ValueError, TypeError, AttributeError):
        return None
    data_type = await _resolve_object_value_data_type(root, info)
    if data_type != "group":
        return None
    orm_group = await info.context.loaders.groups.load(group_id)
    if orm_group is None:
        return None
    return orm_group.name


async def resolve_object_references_from(root, info: Info):
    """Resolve Object.references_from via object_refs_by_from_object.

    D-182-P-06: Despite the requirement prose reading "incoming",
    self-consistent field+loader naming wire this to the FROM/origin side.
    The client gets full from_object/to_object via D-182-P-05 ObjectRef
    either way.  Refs whose to-object is DENY'd are omitted (quick 261011-tv1).

    The per-parent cap runs on the post-visibility list (GQLC-03/D-10), so a
    DENY'd ref neither appears in the truncated window nor consumes a cap
    slot; the truncation is reported as ``references_from`` (D-19).
    """
    orm_refs = await info.context.loaders.object_refs_by_from_object.load(root.id) or []
    visible = await visible_ids(info, "object", [r.to_object_id for r in orm_refs])
    visible_refs = [r for r in orm_refs if r.to_object_id in visible]
    return [
        mappers.object_ref_from_orm(r)
        for r in cap_list(info, "references_from", visible_refs)
    ]


async def resolve_object_references_to(root, info: Info):
    """Resolve Object.references_to via object_refs_by_to_object.

    D-182-P-06: Incoming references (consistent with field+loader naming).
    Refs whose from-object is DENY'd are omitted (quick 261011-tv1).

    The per-parent cap runs on the post-visibility list (GQLC-03/D-10) and
    reports the truncation as ``references_to`` (D-19).
    """
    orm_refs = await info.context.loaders.object_refs_by_to_object.load(root.id) or []
    visible = await visible_ids(info, "object", [r.from_object_id for r in orm_refs])
    visible_refs = [r for r in orm_refs if r.from_object_id in visible]
    return [
        mappers.object_ref_from_orm(r)
        for r in cap_list(info, "references_to", visible_refs)
    ]


async def resolve_object_ref_from_object(root, info: Info):
    """Resolve ObjectRef.from_object from root.from_object_id via objects
    DataLoader.

    D-182-P-05: Expose the full source Object on ObjectRef.  Null on
    record-level DENY (quick 261011-tv1).
    """
    if root.from_object_id is None:
        return None
    orm_obj = await info.context.loaders.objects.load(root.from_object_id)
    if orm_obj is None:
        return None
    if not await record_visible(info, "object", root.from_object_id):
        return None
    return mappers.object_from_orm(orm_obj)


async def resolve_object_ref_to_object(root, info: Info):
    """Resolve ObjectRef.to_object from root.to_object_id via objects DataLoader.

    D-182-P-05: Expose the full target Object on ObjectRef.  Null on
    record-level DENY (quick 261011-tv1).
    """
    if root.to_object_id is None:
        return None
    orm_obj = await info.context.loaders.objects.load(root.to_object_id)
    if orm_obj is None:
        return None
    if not await record_visible(info, "object", root.to_object_id):
        return None
    return mappers.object_from_orm(orm_obj)


async def resolve_object_type_parent(root, info: Info):
    """Resolve ObjectType.parent from root._parent_id.  None-safe.

    Null on record-level DENY: the parent type is not on the record chain,
    so a DENY on it does not hide the child (quick 261011-tv1).
    """
    if root._parent_id is None:
        return None
    orm_ot = await info.context.loaders.object_types.load(root._parent_id)
    if orm_ot is None:
        return None
    if not await record_visible(info, "object_type", root._parent_id):
        return None
    return mappers.object_type_from_orm(orm_ot)


async def resolve_object_type_children(root, info: Info):
    """Resolve ObjectType.children via object_types_by_parent grouped DataLoader.

    DENY'd child types are omitted (quick 261011-tv1). The per-parent cap runs
    on the post-visibility list (GQLC-03/D-10) and reports the truncation as
    ``children`` (D-19).
    """
    orm_types = await info.context.loaders.object_types_by_parent.load(root.id) or []
    visible = await visible_ids(info, "object_type", [t.id for t in orm_types])
    visible_types = [t for t in orm_types if t.id in visible]
    return [
        mappers.object_type_from_orm(t)
        for t in cap_list(info, "children", visible_types)
    ]


async def resolve_object_type_icon(root, info: Info):
    """Resolve ObjectType.icon from root._icon_id.  None-safe — ObjectTypes
    are not required to have an icon.
    """
    if root._icon_id is None:
        return None
    orm_icon = await info.context.loaders.icons.load(root._icon_id)
    if orm_icon is None:
        return None
    # Icon is a flat GraphQL type with no Private attrs or FK edges —
    # construct directly from ORM row rather than adding a mapper function.
    from app.graphql.types.icon import Icon

    return Icon(
        id=orm_icon.id,
        name=orm_icon.name,
        local_name=orm_icon.local_name,
        url=orm_icon.url,
        type=orm_icon.type,
        created_at=orm_icon.created_at,
        updated_at=orm_icon.updated_at,
    )


async def resolve_schema_object_types(root, info: Info):
    """Resolve Schema.object_types via object_types_by_schema grouped DataLoader.

    DENY'd object types are omitted (quick 261011-tv1). The per-parent cap runs
    on the post-visibility list (GQLC-03/D-10) and reports the truncation as
    ``object_types`` (D-19).
    """
    orm_types = await info.context.loaders.object_types_by_schema.load(root.id) or []
    visible = await visible_ids(info, "object_type", [t.id for t in orm_types])
    visible_types = [t for t in orm_types if t.id in visible]
    return [
        mappers.object_type_from_orm(t)
        for t in cap_list(info, "object_types", visible_types)
    ]


async def resolve_inherited_attributes(root, info: Info):
    """Resolve ObjectType.inherited_attributes via the request-scoped
    inherited_attributes grouped DataLoader (WORK-02, D-05/D-06/D-10).

    Replaces the former per-ObjectType direct service call: the loader
    dispatches ``list_object_type_attributes_multi`` — one multi-root
    recursive CTE covering every ObjectType root requested across the
    current request, cache-free (D-08) — and groups rows by root id.

    LOCK-FREE resolver body: the loader's ``_load_fn`` acquires
    ``info.context.lock`` internally around the shared-session dispatch
    (same lock as every other loader in this request). Acquiring it again
    here — or around the ``attribute_types.load_many()`` call below — would
    deadlock, since ``asyncio.Lock`` is not reentrant and graphql-core
    resolves sibling ObjectType fields concurrently via ``asyncio.gather``.

    The per-parent cap (GQLC-03/D-10) is applied to the MAPPED result list —
    the loader already returns exactly the rows for this root — and reports
    the truncation as ``inherited_attributes`` (D-19).
    """
    items = await info.context.loaders.inherited_attributes.load(root.id)

    if not items:
        return []

    # D-06: one load_many() dispatch for the union of attribute_type_ids —
    # replaces the former sequential per-attribute load() loop.
    attr_type_ids = list({
        item["attribute_type_id"] for item in items if item.get("attribute_type_id")
    })
    attr_type_rows = (
        await info.context.loaders.attribute_types.load_many(attr_type_ids)
        if attr_type_ids
        else []
    )
    attr_type_map = {row.id: row for row in attr_type_rows if row is not None}

    # Map CTE dicts → ObjectTypeAttribute, resolving data_type from the
    # batch-loaded AttributeType.
    result = []
    for item in items:
        aid = item.get("attribute_type_id")
        attr_type_row = attr_type_map.get(aid) if aid is not None else None
        # data_type may be a string (ORM column value) or an enum member.
        # Handle both so `data_type.value` doesn't crash on plain strings.
        if attr_type_row is not None:
            raw = getattr(attr_type_row, "data_type", "text")
            db_data_type = raw.value if hasattr(raw, "value") else raw
        else:
            db_data_type = "text"
        # Handle potential enum member vs. string
        if hasattr(db_data_type, "value"):
            db_data_type = db_data_type.value
        result.append(
            mappers.object_type_attribute_from_orm(item, db_data_type)
        )

    return cap_list(info, "inherited_attributes", result)
