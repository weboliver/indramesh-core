import asyncio
import logging
import re
import uuid

from opensearchpy import NotFoundError, RequestError
from sqlalchemy import and_, bindparam, func, or_, select, text
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session_maker
from app.db.opensearch import get_opensearch
from app.db.redis import get_redis
from app.config import settings
from app.models import (
    Object, ObjectType, ObjectTypeAttribute, ObjectValue, AttributeType,
    AttributeTypeDataType, Schema,
)
from app.schemas.crud import SearchHit, SearchResponse
from app.services.cache_service import (
    build_list_cache_key, build_tag_key, get_cached, invalidate_entity_cache,
    set_cached_with_tags,
)
from app.services.record_permission_service import build_record_permission_filter
from app.services.value_visibility import (
    principal_signature,
    resolve_principal_roles,
    visible_attribute_conditions,
)
from app.services.select_option_service import build_option_map, resolve_display_value
from app.services import hook_origin
from app.plugin_system import get_plugin_registry
from app.plugin_system import hooks as plugin_hooks

logger = logging.getLogger(__name__)


def _hook_registry():
    """Return the process-wide PluginRegistry, or None when uninitialized.

    Hook firing is additive-only (PLUGCORE-07): an uninitialized registry (unit
    tests calling these functions without the app/worker lifespan) must never
    break indexing, so ``RuntimeError`` degrades to "no hooks".
    """
    try:
        return get_plugin_registry()
    except RuntimeError:
        return None


async def _fire_index_hook(hook_name: str, db: AsyncSession, **kwargs) -> None:
    """Fire a ``before_index``/``after_index`` hook on the caller's session (D-06)."""
    registry = _hook_registry()
    if registry is None:
        return
    await plugin_hooks.fire_hook(registry, hook_name, active_session=db, **kwargs)


SEARCH_REQUEST_TIMEOUT_SECONDS = 5
"""Ceiling of the per-call Elasticsearch read timeout for the search paths (GQLC-04).

The search read paths run inside an interactive GraphQL operation, so a single
ES call must fail (and degrade to empty results) long before the wall clock
(``GRAPHQL_EXECUTION_TIMEOUT``, 10 s by default) can cut the whole operation
off — and far below nginx's 60 s read timeout. Deliberately a code constant,
NOT an env var: D-09 closes the set of ``GRAPHQL_*`` values, and this is an
implementation budget rather than an operator-facing limit.

The EFFECTIVE bound is ``_search_request_timeout()``: this ceiling combined with
half the configured wall clock, so a deployment that lowers
``GRAPHQL_EXECUTION_TIMEOUT`` below the default cannot end up with an ES read
bounded above the wall clock that is supposed to contain it.
"""


def _search_request_timeout() -> float:
    """The effective per-call ES read bound: the ceiling, capped at half the wall clock.

    Read at CALL time (like every other limit value), so an operator lowering
    ``GRAPHQL_EXECUTION_TIMEOUT`` keeps the "ES fails long before the wall
    clock" invariant without a second knob: with the shipped defaults
    (10 s wall clock) the bound is exactly ``SEARCH_REQUEST_TIMEOUT_SECONDS``.
    """
    return min(SEARCH_REQUEST_TIMEOUT_SECONDS, settings.graphql_execution_timeout / 2)

DATA_TYPE_TO_ES_MAPPING = {
    AttributeTypeDataType.DOUBLE.value: {"type": "float"},
    AttributeTypeDataType.INTEGER.value: {"type": "integer"},
    AttributeTypeDataType.BOOLEAN.value: {"type": "boolean"},
    AttributeTypeDataType.TEXT.value: {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
    AttributeTypeDataType.DATETIME.value: {"type": "date"},
    AttributeTypeDataType.JSON.value: {"type": "flattened"},
    AttributeTypeDataType.STATUS.value: {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
    AttributeTypeDataType.VERSION.value: {"type": "text"},
    AttributeTypeDataType.GROUP.value: {"type": "text"},
    AttributeTypeDataType.USER.value: {"type": "text"},
    AttributeTypeDataType.JIRA.value: {"type": "text"},
    AttributeTypeDataType.CONFLUENCE.value: {"type": "text"},
    AttributeTypeDataType.REFERENCE.value: {"type": "text"},
    AttributeTypeDataType.SELECT.value: {"type": "keyword"},
    AttributeTypeDataType.URL.value: {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
    AttributeTypeDataType.EMAIL.value: {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
    # TEXTAREA (Phase 276) is full-text like TEXT but intentionally maps to the
    # plain fallback shape {"type": "text"} — that is exactly what the
    # un-mapped default in _build_mapping_properties produced before, so this
    # entry only makes the completeness invariant explicit and changes no
    # live index mapping.
    AttributeTypeDataType.TEXTAREA.value: {"type": "text"},
}


def _extract_typed_value(
    value: ObjectValue,
    data_type: str,
    structure: dict | None = None,
    name_map: dict[str, str] | None = None,
) -> any:
    if data_type == "double":
        return value.value_float
    elif data_type == "integer":
        return value.value_integer
    elif data_type == "boolean":
        return value.value_boolean
    elif data_type == "status":
        return (value.value_struct or {}).get("name")
    elif data_type == "select":
        # D-14: index-time resolution — the index stores the BASE display
        # value (never the option id, never a translation). Searching for the
        # display string ("Rot") matches post-migration data (LOOKUP-05);
        # stale/deleted-option uuids index raw (D-15). Sync-pure — the
        # structure is already loaded with the attribute.
        return resolve_display_value(value.value_simple, build_option_map(structure))
    elif data_type in ("user", "group"):
        # D-15: resolve to the user's/group's display name via the
        # execution-local name_map (str(uuid) -> name), preloaded once per
        # reindex snapshot (D-11 scope). Unresolvable UUIDs and non-UUID
        # legacy values (D-07) fall back to the raw stored value — never
        # fabricated. No name_map at all (e.g. the single-object
        # build_es_document path, out of this plan's scope) also falls back
        # to raw — existing indexes refresh on the next bulk_reindex.
        raw = value.value_simple
        if raw is None or not name_map:
            return raw
        try:
            key = str(uuid.UUID(raw))
        except (ValueError, TypeError, AttributeError):
            return raw
        return name_map.get(key, raw)
    elif data_type in ("text", "version", "jira", "confluence", "reference", "url", "email"):
        return value.value_simple
    elif data_type == "datetime":
        return value.value_datetime.isoformat() if value.value_datetime else None
    elif data_type == "json":
        return value.value_struct
    return None


METADATA_FIELDS = {"id", "name", "object_type_id", "schema_id", "source_id", "created_at", "updated_at"}


def _build_attr_map(attrs) -> dict:
    """Build ObjectTypeAttribute.id -> (attr_name, data_type, structure).

    Pure helper shared by the one-object document builder and the per-ObjectType
    reindex snapshot: attribute_type is expected to be eager-loaded, and the
    structure rides along so SELECT values resolve at index time (D-14).
    """
    attr_map = {}
    for attr in attrs:
        at = attr.attribute_type
        if at is not None:
            attr_map[attr.id] = (attr.name, at.data_type, attr.structure)
    return attr_map


def _build_document_from_attr_map(
    obj: Object,
    source_id: str | None,
    attr_map: dict,
    name_map: dict[str, str] | None = None,
) -> dict:
    """Pure snapshot-backed document builder (SYNCB-03, D-09).

    Produces exactly the same application `_source` fields as the semantic
    baseline in `build_es_document()` — metadata fields plus
    `attr_<object_type_id>_<name>`
    values converted via `_extract_typed_value()`. No database reads — the
    optional `name_map` (D-15 user/group name resolution) is supplied as
    already-loaded data by the caller, never fetched here.
    """
    # Build metadata fields
    doc = {
        "id": str(obj.id),
        "name": obj.name,
        # 246 D-07: human-readable IndraMesh key in the index (search/AQL-addressable).
        "key": obj.key,
        "object_type_id": str(obj.object_type_id),
        "schema_id": str(obj.schema_id),
        "source_id": source_id,
        "created_at": obj.created_at.isoformat() if obj.created_at else None,
        "updated_at": obj.updated_at.isoformat() if obj.updated_at else None,
    }

    # Add attribute values
    for value in obj.values:
        entry = attr_map.get(value.object_type_attribute_id)
        if entry is None:
            continue
        attr_name, data_type, structure = entry
        # Always prefix with attr_ to avoid collision with metadata fields
        field_key = f"attr_{obj.object_type_id}_{attr_name}"
        typed_value = _extract_typed_value(value, data_type, structure, name_map)
        if typed_value is not None:
            doc[field_key] = typed_value

    return doc


def _build_mapping_properties(attrs) -> dict:
    """Base metadata properties plus `attr_<object_type_id>_<name>` mappings.

    Pure helper shared by `build_index_mappings()` and the per-ObjectType
    reindex snapshot so the snapshot-derived mappings are identical to the
    existing mapping contract by construction.
    """
    # Base properties for metadata fields
    properties = {
        "id": {"type": "keyword"},
        "name": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
        # 246 D-07: key is a keyword (exact-match/AQL-addressable).
        "key": {"type": "keyword"},
        "object_type_id": {"type": "keyword"},
        "schema_id": {"type": "keyword"},
        "source_id": {"type": "keyword"},
        "created_at": {"type": "date"},
        "updated_at": {"type": "date"},
    }

    # Add attribute field mappings
    # No additional query fired here — attribute_type is already eager-loaded
    for attr in attrs:
        at = attr.attribute_type
        if at is None:
            continue
        es_type = DATA_TYPE_TO_ES_MAPPING.get(at.data_type, {"type": "text"})
        properties[f"attr_{attr.object_type_id}_{attr.name}"] = es_type

    return properties


async def _load_user_group_name_map(db: AsyncSession) -> dict[str, str]:
    """D-15: execution-local str(uuid) -> display-name map for USER/GROUP
    index resolution.

    Built once per reindex snapshot load (never process-wide or Redis-cached
    — same execution-local scope as the rest of `_ReindexSnapshot`, D-11) and
    consumed only by `_extract_typed_value`'s user/group branch. Two plain
    SELECTs; callers only invoke this when the snapshot's attribute set
    actually contains a USER or GROUP data_type.
    """
    from app.models.group import Group
    from app.models.user import User

    name_map: dict[str, str] = {}
    user_result = await db.execute(select(User.id, User.username))
    for user_id, username in user_result.all():
        name_map[str(user_id)] = username
    group_result = await db.execute(select(Group.id, Group.name))
    for group_id, group_name in group_result.all():
        name_map[str(group_id)] = group_name
    return name_map


async def build_es_document(obj: Object, db: AsyncSession) -> dict:
    # Resolve source_id through Schema
    result = await db.execute(select(Schema).where(Schema.id == obj.schema_id))
    schema = result.scalar_one_or_none()
    source_id = str(schema.source_id) if schema else None

    # Load ObjectTypeAttributes for this object_type with AttributeType eager-loaded
    result = await db.execute(
        select(ObjectTypeAttribute)
        .options(selectinload(ObjectTypeAttribute.attribute_type))
        .where(ObjectTypeAttribute.object_type_id == obj.object_type_id)
    )
    attrs = result.scalars().all()

    return _build_document_from_attr_map(obj, source_id, _build_attr_map(attrs))


async def build_index_mappings(db: AsyncSession, object_type_id: uuid.UUID) -> dict:
    # Query ObjectTypeAttributes for this object_type with AttributeType eager-loaded
    result = await db.execute(
        select(ObjectTypeAttribute)
        .options(selectinload(ObjectTypeAttribute.attribute_type))
        .where(ObjectTypeAttribute.object_type_id == object_type_id)
    )
    attrs = result.scalars().all()

    return {"properties": _build_mapping_properties(attrs)}


async def ensure_index(
    db: AsyncSession, schema_id: uuid.UUID, object_type_id: uuid.UUID
) -> bool:
    """Create the Schema's object index if it does not exist yet (D-16).

    Mappings are passed **inline on creation only**: a brand-new index still
    needs them. An already existing index is left alone — the per-document index
    path never writes a mapping. A mapping change belongs to a schema change and
    goes through :func:`refresh_index_mapping`, the single ``put_mapping`` call
    site.
    """
    es = await get_opensearch()
    index_name = f"objects_{schema_id}"
    exists = await es.indices.exists(index=index_name)
    if exists:
        return False
    mappings = await build_index_mappings(db, object_type_id)
    try:
        await es.indices.create(
            index=index_name,
            body={
                "mappings": mappings,
                "settings": {"number_of_shards": 1, "number_of_replicas": 0},
            },
        )
    except RequestError as e:
        # Concurrent index_object_job calls can race here; the other caller
        # already created it — with its own inline mappings. The loser writes
        # nothing: D-16 keeps the document path free of mapping writes.
        if e.error != "resource_already_exists_exception":
            raise
        return False
    return True


async def refresh_index_mapping(object_type_id: uuid.UUID) -> bool:
    """Write one ObjectType's attribute mappings — the ONLY ``put_mapping`` site (D-16).

    Called from the schema-changing write paths (ObjectType and
    ObjectTypeAttribute create/update/delete) after the definition change has
    committed, so a new attribute reaches the index mapping when the schema
    changes and a document write touches no mapping at all.

    Returns ``True`` when a mapping was written. ``False`` is not an error: a
    brand-new index is created by the regular index path with its mappings
    inline, and a just-deleted ObjectType has no definition left to map.
    Best-effort like the rest of the search surface — a mapping failure must not
    turn an already committed schema change into a request error.
    """
    async with async_session_maker() as db:
        result = await db.execute(
            select(ObjectType.schema_id).where(ObjectType.id == object_type_id)
        )
        schema_id = result.scalar_one_or_none()
        if schema_id is None:
            return False
        mappings = await build_index_mappings(db, object_type_id)

    try:
        es = await get_opensearch()
        index_name = f"objects_{schema_id}"
        if not await es.indices.exists(index=index_name):
            return False
        await es.indices.put_mapping(
            index=index_name, body={"properties": mappings["properties"]}
        )
    except Exception:
        logger.warning(
            "search mapping refresh failed for ObjectType %s",
            object_type_id,
            exc_info=True,
        )
        return False
    return True


async def delete_index(schema_id: uuid.UUID) -> bool:
    es = await get_opensearch()
    await es.indices.delete(index=f"objects_{schema_id}", ignore=[404], ignore_unavailable=True)
    return True


async def remove_object_type_documents(
    object_type_id: uuid.UUID, schema_id: uuid.UUID
) -> dict:
    """Remove data documents without touching remove_object_type_from_index().

    The similarly named existing function removes the ObjectType metadata
    document from the separate ``object_types`` catalog index.
    """
    es = await get_opensearch()
    await es.delete_by_query(
        index=f"objects_{schema_id}",
        body={"query": {"term": {"object_type_id": str(object_type_id)}}},
        refresh=True,
        conflicts="proceed",
        ignore=[404],
    )
    return {"success": True, "object_type_id": str(object_type_id)}


async def _build_es_document_safe(
    db: AsyncSession, obj: Object
) -> dict:
    """Safely build ES document by ensuring all relationships are eagerly loaded."""
    await db.refresh(obj, ['values'])
    return await build_es_document(obj, db)


async def index_object(
    object_id: uuid.UUID,
    *,
    actor_id: uuid.UUID | None = None,
    origin: hook_origin.MutationOrigin | None = None,
) -> dict:
    """Index one Object into Elasticsearch, attributing the index hooks.

    ``actor_id``/``origin`` are internal, caller-supplied mutation provenance
    (CORESEAM-11/D-11): the ARQ worker resolves them from the persisted outbox
    row so ``before_index``/``after_index`` name the write initiator, never the
    worker. They are optional and keyword-only — a direct/taskless call gets the
    explicit system shape from the worker, and the ES document shape is
    untouched.
    """
    async with async_session_maker() as db:
        result = await db.execute(
            select(Object)
            .where(Object.id == object_id)
            .options(
                selectinload(Object.values),
                selectinload(Object.object_type),
            )
        )
        obj = result.unique().scalar_one_or_none()
        if not obj:
            return {"success": False, "error": "Object not found"}
        doc = await _build_es_document_safe(db, obj)
        await ensure_index(db, obj.schema_id, obj.object_type_id)
        es = await get_opensearch()
        # PLUGCORE-07: before_index/after_index around the single-object write,
        # sharing this function's own already-open session (D-06).
        origin_payload = origin.as_payload() if origin is not None else None
        await _fire_index_hook(
            "before_index", db, object_id=object_id, schema_id=obj.schema_id,
            document=doc, actor_id=actor_id, origin=origin_payload,
        )
        await es.index(index=f"objects_{obj.schema_id}", id=str(obj.id), body=doc)
        await _fire_index_hook(
            "after_index", db, object_id=object_id, schema_id=obj.schema_id,
            actor_id=actor_id, origin=origin_payload,
        )
        return {"success": True, "object_id": str(object_id)}


async def remove_object_from_index(
    object_id: uuid.UUID,
    object_type_id: uuid.UUID | None = None,
    schema_id: uuid.UUID | None = None,
) -> dict:
    if schema_id is None:
        async with async_session_maker() as db:
            result = await db.execute(select(Object).where(Object.id == object_id))
            obj = result.scalar_one_or_none()
            if obj:
                object_type_id = obj.object_type_id
                schema_id = obj.schema_id
    if schema_id is None:
        return {"success": False, "error": "Object not found"}
    es = await get_opensearch()
    await es.delete(index=f"objects_{schema_id}", id=str(object_id), ignore=[404])
    return {"success": True, "object_id": str(object_id)}


# ---- ObjectType ES sync (D-20) ----

OBJECT_TYPES_INDEX = "object_types"

async def ensure_object_types_index() -> bool:
    """Create the static object_types ES index with mapping if it doesn't exist."""
    es = await get_opensearch()
    exists = await es.indices.exists(index=OBJECT_TYPES_INDEX)
    if not exists:
        await es.indices.create(
            index=OBJECT_TYPES_INDEX,
            body={
                "mappings": {
                    "properties": {
                        "id": {"type": "keyword"},
                        "name": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                        "description": {"type": "text"},
                        "schema_id": {"type": "keyword"},
                        "created_at": {"type": "date"},
                        "updated_at": {"type": "date"},
                    }
                },
                "settings": {"number_of_shards": 1, "number_of_replicas": 0},
            },
        )
        return True
    return False


async def index_object_type(object_type_id: uuid.UUID) -> dict:
    """Index an ObjectType in Elasticsearch."""
    from app.models.object_type import ObjectType as ObjectTypeModel

    async with async_session_maker() as db:
        result = await db.execute(
            select(ObjectTypeModel).where(ObjectTypeModel.id == object_type_id)
        )
        ot = result.scalar_one_or_none()
        if not ot:
            return {"success": False, "error": "ObjectType not found"}

        await ensure_object_types_index()
        es = await get_opensearch()
        doc = {
            "id": str(ot.id),
            "name": ot.name,
            "description": ot.description or "",
            "schema_id": str(ot.schema_id),
            "created_at": ot.created_at.isoformat() if ot.created_at else None,
            "updated_at": ot.updated_at.isoformat() if ot.updated_at else None,
        }
        await es.index(index=OBJECT_TYPES_INDEX, id=str(ot.id), body=doc)
        return {"success": True, "object_type_id": str(object_type_id)}


async def remove_object_type_from_index(object_type_id: uuid.UUID) -> dict:
    """Remove an ObjectType from Elasticsearch."""
    es = await get_opensearch()
    await es.delete(index=OBJECT_TYPES_INDEX, id=str(object_type_id), ignore=[404])
    return {"success": True, "object_type_id": str(object_type_id)}


# ---- Schema ES sync (D-20) ----

SCHEMAS_INDEX = "schemas"

async def ensure_schemas_index() -> bool:
    """Create the static schemas ES index with mapping if it doesn't exist."""
    es = await get_opensearch()
    exists = await es.indices.exists(index=SCHEMAS_INDEX)
    if not exists:
        await es.indices.create(
            index=SCHEMAS_INDEX,
            body={
                "mappings": {
                    "properties": {
                        "id": {"type": "keyword"},
                        "name": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                        "description": {"type": "text"},
                        "source_id": {"type": "keyword"},
                        "created_at": {"type": "date"},
                        "updated_at": {"type": "date"},
                    }
                },
                "settings": {"number_of_shards": 1, "number_of_replicas": 0},
            },
        )
        return True
    return False


async def index_schema(schema_id: uuid.UUID) -> dict:
    """Index a Schema in Elasticsearch."""
    from app.models.schema import Schema as SchemaModel

    async with async_session_maker() as db:
        result = await db.execute(
            select(SchemaModel).where(SchemaModel.id == schema_id)
        )
        schema = result.scalar_one_or_none()
        if not schema:
            return {"success": False, "error": "Schema not found"}

        await ensure_schemas_index()
        es = await get_opensearch()
        doc = {
            "id": str(schema.id),
            "name": schema.name,
            "description": schema.description or "",
            "source_id": str(schema.source_id),
            "created_at": schema.created_at.isoformat() if schema.created_at else None,
            "updated_at": schema.updated_at.isoformat() if schema.updated_at else None,
        }
        await es.index(index=SCHEMAS_INDEX, id=str(schema.id), body=doc)
        return {"success": True, "schema_id": str(schema_id)}


async def remove_schema_from_index(schema_id: uuid.UUID) -> dict:
    """Remove a Schema from Elasticsearch."""
    es = await get_opensearch()
    await es.delete(index=SCHEMAS_INDEX, id=str(schema_id), ignore=[404])
    return {"success": True, "schema_id": str(schema_id)}


def _transform_highlights(highlights: dict | None) -> dict | None:
    """Transform ES <em> tags to <mark> tags and strip all other HTML.
    
    ES returns highlighted snippets with <em>...</em> around matched terms.
    This function strips any raw HTML that might exist in the source text,
    then replaces only the ES <em> tags with <mark> for safe frontend rendering.
    
    Per D-11: server-side transformation so frontend receives already-safe <mark> snippets.
    """
    if not highlights:
        return None
    transformed = {}
    for field, snippets in highlights.items():
        if not isinstance(snippets, list):
            continue
        cleaned = []
        for snippet in snippets:
            if not isinstance(snippet, str):
                continue
            # 1. Strip all HTML tags except bare <em>/</em> (preserve ES highlight markers).
            #    The negative lookahead anchors on 'em>' / '/em>' so only the exact
            #    bare tags ES produces survive — attacker-crafted tags like
            #    <em onmouseover="..."> are stripped because the 'em' is not
            #    immediately followed by '>'.
            stripped = re.sub(r'<(?!/?em>)[^>]*>', '', snippet)
            # 2. Replace <em> with <mark>, </em> with </mark>
            marked = stripped.replace('<em>', '<mark>').replace('</em>', '</mark>')
            cleaned.append(marked)
        if cleaned:
            transformed[field] = cleaned
    return transformed if transformed else None


SEARCH_CACHE_ENTITY = "search"

SEARCH_DENIED_IDS_TERMS_LIMIT = 4096
"""Largest denied-id set still sent as one ``terms`` clause (D-07).

Above it the (smaller) visible-id set becomes the restriction instead. Both
branches are query-side, so the OpenSearch ``total`` — which the response and
the cache payload carry — can never count a record the caller may not read.
"""


def _inverted_filter_clause(clause, role_ids):
    """``NOT (<fragment>)`` — the rows the record predicate excludes.

    ``build_record_permission_filter`` returns its own generated ``text()``
    fragment (never caller input) carrying a single ``:role_ids`` bindparam.
    This SQLAlchemy version cannot group a ``TextClause`` under ``not_()``, so
    the complement is the same fragment wrapped in ``NOT (...)`` — exactly the
    negation of the builder's "not denied" clause, with no second DENY
    semantics to drift from.
    """
    return text(f"NOT ({clause.text})").bindparams(
        bindparam("role_ids", value=role_ids)
    )


async def _record_denied_ids(
    db: AsyncSession, user_id: str, entity_type: str, model, role_ids
) -> list[uuid.UUID]:
    """Ids the caller's record predicate excludes (the query-side filter input).

    ``[]`` when the principal has no roles at all: the DENY-only model is
    permissive without roles, so such a caller sees every record the type-level
    gate already allowed.
    """
    clause = await build_record_permission_filter(
        db, user_id, entity_type, is_superuser=False, role_ids=role_ids
    )
    if clause is None:
        return []
    result = await db.execute(
        select(model.id).where(_inverted_filter_clause(clause, role_ids))
    )
    return [row[0] for row in result.all()]


async def _record_visible_ids(
    db: AsyncSession, user_id: str, entity_type: str, model, role_ids
) -> list[uuid.UUID]:
    """Ids the caller's record predicate keeps — the large-denied-set fallback."""
    clause = await build_record_permission_filter(
        db, user_id, entity_type, is_superuser=False, role_ids=role_ids
    )
    statement = select(model.id)
    if clause is not None:
        statement = statement.where(clause)
    result = await db.execute(statement)
    return [row[0] for row in result.all()]


async def _es_id_restriction(
    db: AsyncSession, user_id: str, entity_type: str, model, role_ids
) -> dict:
    """The bool-query part restricting an OpenSearch read to visible ids (D-07).

    Normal form: ``must_not`` over the caller's **denied** ids (that set is
    normally small). Fallback for a pathologically large denied set: a
    ``filter`` ``terms`` restriction over the visible ids. The branch in force
    is named in the returned dict's caller, and neither branch filters hits
    after the query — ``total`` comes from OpenSearch itself.
    """
    denied = await _record_denied_ids(db, user_id, entity_type, model, role_ids)
    if not denied:
        return {}
    if len(denied) <= SEARCH_DENIED_IDS_TERMS_LIMIT:
        # Active branch: exclude the denied ids.
        return {"must_not": [{"terms": {"id": [str(i) for i in denied]}}]}
    # Fallback branch: the denied set is too large for one terms clause, so
    # restrict on the visible set instead.
    visible = await _record_visible_ids(db, user_id, entity_type, model, role_ids)
    return {"filter": [{"terms": {"id": [str(i) for i in visible]}}]}


def _merge_query_restriction(search_query: dict, restriction: dict) -> dict:
    """Wrap ``search_query`` in a bool together with a visibility restriction."""
    if not restriction:
        return search_query
    bool_query: dict = {"must": [search_query]}
    if restriction.get("filter"):
        bool_query["filter"] = list(restriction["filter"])
    if restriction.get("must_not"):
        bool_query["must_not"] = list(restriction["must_not"])
    return {"bool": bool_query}


def _strip_blocked_attribute_fields(
    hit: dict, blocked_names: dict[str, set[str]]
) -> None:
    """Drop blocked ``attr_<object_type_id>_<name>`` fields from one hit (D-07).

    Removes the field from ``_source`` **and** ``highlight`` in place, before a
    ``SearchHit`` is built and before the payload can reach the cache. The
    index format is untouched — a hidden or attribute-type-DENY'd attribute is
    still indexed, it just never leaves the service.
    """
    source = hit.get("_source")
    if not isinstance(source, dict):
        return
    object_type_id = source.get("object_type_id")
    names = blocked_names.get(str(object_type_id)) if object_type_id else None
    if not names:
        return
    for name in names:
        field = f"attr_{object_type_id}_{name}"
        source.pop(field, None)
        highlights = hit.get("highlight")
        if isinstance(highlights, dict):
            highlights.pop(field, None)


async def _blocked_attribute_names(
    db: AsyncSession,
    object_type_ids,
    user_id: str,
    is_superuser: bool,
    role_ids,
) -> dict[str, set[str]]:
    """``object_type_id`` (str) -> attribute names this principal may not see.

    One ``ObjectTypeAttribute`` read per page for every distinct ObjectType in
    it: the ``is_hidden`` half and the attribute-type DENY half of the value
    predicate, resolved once (RLR-08). A superuser gets ``{}`` and no query
    (RLR-05 parity), so nothing is stripped for them.
    """
    ids = {object_type_id for object_type_id in object_type_ids if object_type_id}
    if is_superuser or not ids:
        return {}

    denied_attribute_types = await _record_denied_ids(
        db, user_id, "attribute_type", AttributeType, role_ids
    )
    conditions = [ObjectTypeAttribute.is_hidden.is_(True)]
    if denied_attribute_types:
        conditions.append(
            ObjectTypeAttribute.attribute_type_id.in_(denied_attribute_types)
        )
    result = await db.execute(
        select(ObjectTypeAttribute.object_type_id, ObjectTypeAttribute.name).where(
            ObjectTypeAttribute.object_type_id.in_(ids), or_(*conditions)
        )
    )
    blocked: dict[str, set[str]] = {}
    for ota_object_type_id, name in result.all():
        blocked.setdefault(str(ota_object_type_id), set()).add(name)
    return blocked


async def _allowed_attribute_fields(
    db: AsyncSession,
    user_id: str,
    role_ids,
    *,
    object_type_id: uuid.UUID | None = None,
    schema_id: uuid.UUID | None = None,
    source_id: uuid.UUID | None = None,
) -> dict[uuid.UUID, list[str]]:
    """Allowed ``attr_<object_type_id>_<name>`` fields, grouped by schema.

    The search query has to name only the attribute fields a principal may see;
    the ``attr_*`` wildcard would let OpenSearch's ``total`` answer whether a
    hidden or attribute-type-DENY'd value contains a term even though
    ``_source``/highlights are stripped afterwards (WR-03). The predicate is the
    same name-space half of the value predicate AQL name resolution uses
    (``visible_attribute_conditions``): ``is_hidden`` plus the attribute-type
    record DENY, joined on ``attribute_types``.

    Grouping is ``(object_type_id, name, schema_id)`` with
    ``HAVING bool_and(<predicate>)``: a name that occurs twice on one ObjectType
    is admitted only when **every** row passes (fail-closed), and the caller
    gets one field list per schema — the per-ObjectType restriction, because
    every index field name embeds its ObjectType id.

    Names containing ``*`` (a field pattern that could expand to a hidden
    sibling) or ``^`` (the boost separator) are dropped, fail-closed.
    Every schema in scope appears in the result, with an empty list when it
    carries no allowed attribute.

    Never called for a superuser — callers pass ``None`` for them so the
    wildcard query and its zero extra queries stay exactly as they were.
    """
    if object_type_id is not None:
        schema_stmt = select(ObjectType.schema_id).where(ObjectType.id == object_type_id)
    elif schema_id is not None:
        schema_stmt = select(Schema.id).where(Schema.id == schema_id)
    elif source_id is not None:
        schema_stmt = select(Schema.id).where(Schema.source_id == source_id)
    else:
        schema_stmt = select(Schema.id)
    schema_ids = list((await db.execute(schema_stmt)).scalars().all())
    fields_by_schema: dict[uuid.UUID, list[str]] = {sid: [] for sid in schema_ids}
    if not schema_ids:
        return fields_by_schema

    conditions = await visible_attribute_conditions(
        db, user_id, False, role_ids=role_ids
    )
    stmt = (
        select(
            ObjectTypeAttribute.object_type_id,
            ObjectTypeAttribute.name,
            ObjectType.schema_id,
        )
        .join(
            AttributeType,
            ObjectTypeAttribute.attribute_type_id == AttributeType.id,
        )
        .join(ObjectType, ObjectType.id == ObjectTypeAttribute.object_type_id)
        .where(ObjectType.schema_id.in_(schema_ids))
        .group_by(
            ObjectTypeAttribute.object_type_id,
            ObjectTypeAttribute.name,
            ObjectType.schema_id,
        )
    )
    if object_type_id is not None:
        stmt = stmt.where(ObjectTypeAttribute.object_type_id == object_type_id)
    if conditions:
        stmt = stmt.having(func.bool_and(and_(*conditions)))

    for ot_id, name, ot_schema_id in (await db.execute(stmt)).all():
        if "*" in name or "^" in name:
            continue
        fields_by_schema.setdefault(ot_schema_id, []).append(f"attr_{ot_id}_{name}")
    for fields in fields_by_schema.values():
        fields.sort()
    return fields_by_schema


def _object_search_query(
    q_es: str, fields_by_schema: dict[uuid.UUID, list[str]] | None
) -> dict:
    """The object-search clause naming exactly the fields the caller may match.

    ``fields_by_schema is None`` is the superuser branch: the unchanged
    ``name^3`` + ``attr_*`` wildcard, byte-identical to the pre-WR-03 query.

    For a restricted caller the wildcard is replaced by the explicit
    ``attr_<object_type_id>_<name>`` list from ``_allowed_attribute_fields``:

    - one schema -> one ``simple_query_string`` over its allowed fields;
    - several schemas (an unscoped search) -> ``bool.should`` with one clause
      per schema that has allowed fields, each carrying a ``schema_id`` term
      filter, so a clause names no more fields than the wildcard expands to on
      that schema's index today;
    - all schemas without allowed fields share **one** clause (a ``terms``
      filter), so the clause count grows with attribute-carrying schemas, not
      with every schema;
    - no schema in scope -> ``name^3`` only.

    ``name^3`` and the attribute fields stay in ONE ``simple_query_string`` per
    clause, so required/OR term semantics across name and attributes are
    unchanged.
    """

    def _clause(fields: list[str]) -> dict:
        return {
            "simple_query_string": {
                "query": q_es,
                "fields": ["name^3", *fields],
                "default_operator": "OR",
                "lenient": True,
            }
        }

    if fields_by_schema is None:
        return _clause(["attr_*"])
    if not fields_by_schema:
        return _clause([])
    if len(fields_by_schema) == 1:
        return _clause(next(iter(fields_by_schema.values())))

    with_fields = [
        (sid, fields) for sid, fields in fields_by_schema.items() if fields
    ]
    without_fields = [
        str(sid) for sid, fields in fields_by_schema.items() if not fields
    ]
    should = [
        {
            "bool": {
                "filter": [{"term": {"schema_id": str(sid)}}],
                "must": [_clause(fields)],
            }
        }
        for sid, fields in with_fields
    ]
    if without_fields:
        should.append(
            {
                "bool": {
                    "filter": [{"terms": {"schema_id": without_fields}}],
                    "must": [_clause([])],
                }
            }
        )
    return {"bool": {"should": should, "minimum_should_match": 1}}


def _hit_object_type_ids(hits) -> set[uuid.UUID]:
    """Parse the ObjectType ids out of ES hits (malformed values skipped)."""
    ids: set[uuid.UUID] = set()
    for hit in hits:
        raw = (hit.get("_source") or {}).get("object_type_id")
        try:
            ids.add(uuid.UUID(str(raw)))
        except (ValueError, TypeError, AttributeError):
            continue
    return ids


def _build_search_cache_key(
    q: str,
    object_type_id: uuid.UUID | None = None,
    schema_id: uuid.UUID | None = None,
    source_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 20,
    *,
    principal: str,
) -> str:
    """Cache key for one query **and one principal** (D-07).

    ``principal`` is required: a payload produced under one principal's record
    filter must never be replayed to another. It is derived from the resolved
    role ids *after* the permission resolution (see
    ``value_visibility.principal_signature``), so two principals with
    different DENY sets cannot collide.
    """
    return build_list_cache_key(
        "search",
        q=q.strip().lower(),  # normalize: case-insensitive
        object_type_id=str(object_type_id) if object_type_id else None,
        schema_id=str(schema_id) if schema_id else None,
        source_id=str(source_id) if source_id else None,
        page=page,
        page_size=page_size,
        principal=principal,
    )


async def search_objects(
    db: AsyncSession,
    q: str,
    object_type_id: uuid.UUID | None = None,
    schema_id: uuid.UUID | None = None,
    source_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 20,
    *,
    user_id: str | None,
    is_superuser: bool,
) -> SearchResponse:
    # --- Principal resolution happens BEFORE the cache key (D-07) ---
    # The key carries a principal component derived from the resolved role ids,
    # so two principals with different DENY sets can never share an entry, and
    # the restriction below is computed from the same resolution. Required
    # keyword-only: a caller that forgets these fails at call time instead of
    # searching unfiltered.
    role_ids = await resolve_principal_roles(db, user_id, is_superuser)
    principal = principal_signature(user_id, role_ids, is_superuser=is_superuser)

    # --- Cache-aside: check cache first ---
    cache_key = _build_search_cache_key(
        q, object_type_id, schema_id, source_id, page, page_size,
        principal=principal,
    )
    cached = await get_cached(cache_key)
    if cached is not None:
        return SearchResponse(**cached)

    # --- Stampede lock (D-06): acquire BEFORE the ES query ---
    # Single-flight: only the lock holder executes the ES query. Concurrent
    # losers do one short bounded wait + cache re-check, then fall through
    # and query directly rather than blocking indefinitely — a request that
    # can't get the lock must still get an answer.
    acquired = False
    try:
        redis = await get_redis()
        lock_key = f"lock:{cache_key}"
        acquired = await redis.set(lock_key, "1", nx=True, ex=5)  # 5s lock expiry
        if not acquired:
            await asyncio.sleep(0.05)
            cached = await get_cached(cache_key)
            if cached is not None:
                return SearchResponse(**cached)
    except Exception:
        # Redis unavailable — fall through to ES query without caching
        pass
    # --- End cache check ---

    resolved_schema_id = schema_id
    if object_type_id is not None:
        from app.models.object_type import ObjectType

        object_type_result = await db.execute(
            select(ObjectType.schema_id).where(ObjectType.id == object_type_id)
        )
        actual_schema_id = object_type_result.scalar_one_or_none()

        if actual_schema_id is None or (schema_id is not None and actual_schema_id != schema_id):
            # Nonexistent ObjectType, or a real ObjectType that belongs to a
            # DIFFERENT Schema than the one supplied — closes 248-VERIFICATION.md
            # Gap 1 / CR-01: never fall through to a sibling-ObjectType or
            # unfiltered objects_* scan for a mismatched/nonexistent pair.
            return SearchResponse(query=q, total=0, page=page, page_size=page_size, results=[])

        resolved_schema_id = actual_schema_id

    if resolved_schema_id is not None:
        indices = [f"objects_{resolved_schema_id}"]
    else:
        indices = ["objects_*"]

    # Phase 97 / SEARCH-01 (closes F-88-02-09) — two independent measures:
    # 1. simple_query_string drops fielded queries (field:value), regex
    #    (/re/), and Boolean precedence (AND/NOT/()) at parse-time, while
    #    keeping the UX operators (+required -excluded, "phrase", | OR).
    # 2. Stripping literal "*" from the ES-bound query closes the wildcard
    #    vector that simple_query_string does NOT close on its own — a bare
    #    "*" resolves to Lucene `*:*` (MatchAllDocsQuery) and "*term" still
    #    executes as a genuine WildcardQuery against .keyword sub-fields,
    #    verified against the live ES 8.11.0 cluster, independent of the
    #    `flags` parameter. `q` itself (unstripped) is still echoed back in
    #    the response so the API's displayed query string is unaffected.
    q_es = q.replace("*", "")
    # Build ES bool query. A restricted caller's clause names only the attribute
    # fields this principal may see (WR-03): the ``attr_*`` wildcard would let
    # OpenSearch's ``total`` answer whether a hidden or attribute-type-DENY'd
    # value contains the term, even though ``_source``/highlights are sanitised
    # afterwards. A superuser keeps the wildcard and pays no extra query.
    fields_by_schema = None
    if not is_superuser:
        fields_by_schema = await _allowed_attribute_fields(
            db,
            user_id,
            role_ids,
            object_type_id=object_type_id,
            schema_id=resolved_schema_id,
            source_id=source_id,
        )
    search_query = _object_search_query(q_es, fields_by_schema)

    filters = []
    if object_type_id:
        filters.append({"term": {"object_type_id": str(object_type_id)}})
    if schema_id:
        filters.append({"term": {"schema_id": str(schema_id)}})
    if source_id:
        filters.append({"term": {"source_id": str(source_id)}})

    # Query-side record restriction (D-07): the caller's denied object ids go
    # into the bool query, so the ES ``total`` this response and its cache
    # payload carry already excludes them. Never a post-query hit filter, and
    # never a filter applied to a payload read back from the cache.
    bool_query: dict = {"must": [search_query]}
    if filters:
        bool_query["filter"] = list(filters)
    if not is_superuser:
        restriction = await _es_id_restriction(
            db, user_id, "object", Object, role_ids
        )
        if restriction.get("filter"):
            bool_query.setdefault("filter", []).extend(restriction["filter"])
        if restriction.get("must_not"):
            bool_query["must_not"] = list(restriction["must_not"])
    if len(bool_query) > 1:
        search_query = {"bool": bool_query}

    try:
        # Per-call bound (GQLC-04): `request_timeout` bounds this one read, and
        # the shared client is built with `max_retries=0` (app.db.opensearch) —
        # the transport retry count is a client-level setting in opensearch-py,
        # so no retry loop can multiply this read past the GraphQL wall clock.
        es = await get_opensearch()
        resp = await es.search(
            index=indices,
            body={
                "query": search_query,
                "from": (page - 1) * page_size,
                "size": page_size,
                "highlight": {
                    "fields": {
                        "name": {},
                        "attr_*": {},
                    },
                    "pre_tags": ["<em>"],
                    "post_tags": ["</em>"],
                    "fragmenter": "unified",
                    "fragment_size": 100,
                    "number_of_fragments": 1,
                },
            },
            request_timeout=_search_request_timeout(),
        )
    except NotFoundError:
        # No index matches (nothing indexed yet / an index not created for this
        # schema): a genuine "no matches" answer, never a degradation.
        return SearchResponse(query=q, total=0, page=page, page_size=page_size, results=[])
    except Exception:
        # ES error — empty results (graceful — Pitfall 4). The result is marked
        # degraded (WR-06): the per-call bound makes a slow or unreachable
        # Elasticsearch answer within seconds, and without the marker the CALLER
        # could not tell that answer from an authoritative "no matches" — the
        # silently-wrong result D-19/D-20 exist to avoid. Mirrors the
        # ``_degraded`` marker search_cross_entity hands its resolver.
        return SearchResponse(
            query=q,
            total=0,
            page=page,
            page_size=page_size,
            results=[],
            degraded=True,
        )

    hits = resp["hits"]["hits"]
    total = resp["hits"]["total"]["value"]

    # Sanitise BEFORE a SearchHit is built and therefore before the cache
    # write: a denied or hidden attribute's value must not survive in
    # ``_source`` or in a highlight snippet (D-07), and the cached payload has
    # to be the sanitised one — never filtered after being read back.
    if not is_superuser:
        blocked_names = await _blocked_attribute_names(
            db, _hit_object_type_ids(hits), user_id, is_superuser, role_ids
        )
        for hit in hits:
            _strip_blocked_attribute_fields(hit, blocked_names)

    results = [
        SearchHit(
            id=uuid.UUID(hit["_source"]["id"]),
            name=hit["_source"]["name"],
            object_type_id=uuid.UUID(hit["_source"]["object_type_id"]),
            schema_id=uuid.UUID(hit["_source"]["schema_id"]),
            score=hit["_score"] or 0.0,
            highlights=_transform_highlights(hit.get("highlight")),
        )
        for hit in hits
    ]

    # --- Cache results if we held the stampede lock ---
    search_data = {
        "query": q,
        "total": total,
        "page": page,
        "page_size": page_size,
        "results": [r.model_dump() for r in results],
    }
    if acquired:
        await set_cached_with_tags(
            cache_key, search_data, tags=[build_tag_key(SEARCH_CACHE_ENTITY)], ttl=10,
        )
        try:
            await redis.delete(lock_key)
        except Exception:
            pass  # Lock auto-expires after 5s — cleanup is best-effort per CR-01
    # If we never held the lock, another worker already populated (or is
    # populating) the cache — nothing to write here.

    return SearchResponse(query=q, total=total, page=page, page_size=page_size, results=results)


async def search_cross_entity(
    q: str,
    page: int = 1,
    page_size: int = 5,
    *,
    user_id: str | None,
    is_superuser: bool,
) -> dict:
    """Cross-entity search across Objects, ObjectTypes, and Schemas via ES _msearch.

    Per D-18: uses _msearch for a single HTTP round-trip across 3 index patterns.
    Per D-15: returns top page_size results per type.
    Per Pitfall 4: uses ignore_unavailable=true for indices that don't exist yet.

    Per D-07 every leg is restricted **inside its query** to the ids the caller
    may read (objects, object types and schemas each resolve their own record
    predicate), and the object leg's hits are sanitised before they are parsed —
    so neither a count nor a highlight carries a record or attribute value the
    caller may not read. ``user_id``/``is_superuser`` are required keyword-only
    arguments: a caller that forgets them fails at call time.

    Returns dict with keys: objects, object_types, schemas — each a dict { count, results }.
    """
    try:
        # Per-call bound (GQLC-04) — see SEARCH_REQUEST_TIMEOUT_SECONDS and the
        # matching bound in search_objects: one OpenSearch read must not outlive
        # the wall clock, and the client-level `max_retries=0` keeps a transport
        # retry from multiplying it.
        es = await get_opensearch()
    except RuntimeError:
        # ES not initialized — return empty results (graceful degradation).
        # _degraded signals callers (e.g. the GraphQL resolver) that this is
        # an ES outage, not "zero matches" — ignored by REST callers that
        # only read the objects/object_types/schemas keys.
        return {
            "objects": {"count": 0, "results": []},
            "object_types": {"count": 0, "results": []},
            "schemas": {"count": 0, "results": []},
            "_degraded": True,
        }

    # Per-leg record restriction (D-07). This route caches nothing, but its
    # ``count`` is the ES total, so the restriction has to be query-side; a
    # post-query hit filter would leave the count itself as an oracle. The
    # service has no request session of its own, so the two short reads use
    # their own (read-only) session — same shape as index_object().
    from app.models.object_type import ObjectType as ObjectTypeModel

    role_ids: list[uuid.UUID] | None = None
    restrictions: dict[str, dict] = {}
    object_fields_by_schema: dict[uuid.UUID, list[str]] | None = None
    if not is_superuser:
        async with async_session_maker() as restriction_db:
            role_ids = await resolve_principal_roles(
                restriction_db, user_id, is_superuser
            )
            restrictions["objects"] = await _es_id_restriction(
                restriction_db, user_id, "object", Object, role_ids
            )
            restrictions["object_types"] = await _es_id_restriction(
                restriction_db, user_id, "object_type", ObjectTypeModel, role_ids
            )
            restrictions["schemas"] = await _es_id_restriction(
                restriction_db, user_id, "schema", Schema, role_ids
            )
            # WR-03: the object leg names only the attribute fields this
            # principal may see, unscoped (every schema) — the same predicate
            # as the /search path. ObjectType/Schema legs search name and
            # description only, so they keep their fields.
            object_fields_by_schema = await _allowed_attribute_fields(
                restriction_db, user_id, role_ids
            )

    # Build msearch searches list — flat alternating header/body dicts (NOT {header,body} pairs;
    # es.msearch(body=...) serializes each top-level list element as one NDJSON line, so the
    # flat header/body/header/body/... ordering from the old NDJSON string must be preserved).
    searches = []

    # Phase 97 / SEARCH-01 (closes F-88-02-09) — see search_objects() above
    # for the full rationale. q_es feeds all 3 ES query bodies below; the
    # unstripped q is still returned as the echoed query string.
    q_es = q.replace("*", "")

    def _restricted(leg: str, query: dict) -> dict:
        return _merge_query_restriction(query, restrictions.get(leg, {}))

    # 1. Objects search (all object_type indices)
    searches.append({"index": "objects_*", "ignore_unavailable": True})
    searches.append({
        "query": _restricted("objects", _object_search_query(q_es, object_fields_by_schema)),
        "from": (page - 1) * page_size,
        "size": page_size,
        "highlight": {"fields": {"name": {}, "attr_*": {}}, "pre_tags": ["<em>"], "post_tags": ["</em>"], "fragmenter": "unified", "fragment_size": 100, "number_of_fragments": 1},
    })

    # 2. ObjectTypes search
    searches.append({"index": "object_types", "ignore_unavailable": True})
    searches.append({
        "query": _restricted("object_types", {"simple_query_string": {"query": q_es, "fields": ["name^3", "description"], "default_operator": "OR", "lenient": True}}),
        "from": (page - 1) * page_size,
        "size": page_size,
        "highlight": {"fields": {"name": {}, "description": {}}, "pre_tags": ["<em>"], "post_tags": ["</em>"], "fragmenter": "unified", "fragment_size": 100, "number_of_fragments": 1},
    })

    # 3. Schemas search
    searches.append({"index": "schemas", "ignore_unavailable": True})
    searches.append({
        "query": _restricted("schemas", {"simple_query_string": {"query": q_es, "fields": ["name^3", "description"], "default_operator": "OR", "lenient": True}}),
        "from": (page - 1) * page_size,
        "size": page_size,
        "highlight": {"fields": {"name": {}, "description": {}}, "pre_tags": ["<em>"], "post_tags": ["</em>"], "fragmenter": "unified", "fragment_size": 100, "number_of_fragments": 1},
    })

    try:
        resp = await es.msearch(body=searches, request_timeout=_search_request_timeout())
    except Exception:
        # ES error — empty results (graceful — Pitfall 4). See _degraded note above.
        return {
            "objects": {"count": 0, "results": []},
            "object_types": {"count": 0, "results": []},
            "schemas": {"count": 0, "results": []},
            "_degraded": True,
        }

    responses = resp.get("responses", [])

    # Sanitise the object leg's hits before they are parsed (D-07): the field
    # set depends on the page's ObjectTypes, so it is resolved once, here.
    object_response = responses[0] if len(responses) > 0 else None
    blocked_names: dict[str, set[str]] = {}
    if not is_superuser and object_response and object_response.get("hits"):
        object_hits = object_response["hits"].get("hits") or []
        if object_hits:
            async with async_session_maker() as sanitise_db:
                blocked_names = await _blocked_attribute_names(
                    sanitise_db,
                    _hit_object_type_ids(object_hits),
                    user_id,
                    is_superuser,
                    role_ids,
                )
            for hit in object_hits:
                _strip_blocked_attribute_fields(hit, blocked_names)

    def _parse_hits(es_response, entity_type):
        """Parse ES response hits into CrossEntityResult-compatible dicts."""
        if not es_response or "hits" not in es_response:
            return {"count": 0, "results": []}
        hits = es_response["hits"]["hits"]
        total = es_response["hits"]["total"]["value"] if isinstance(es_response["hits"]["total"], dict) else es_response["hits"]["total"]
        results = []
        for hit in hits:
            src = hit["_source"]
            result = {
                "id": src["id"],
                "name": src.get("name", ""),
                "type": entity_type,
                "description": src.get("description"),
                "url": _entity_url(entity_type, src),
                "highlights": _transform_highlights(hit.get("highlight")),
                # FK IDs for GraphQL SearchHit entity resolvers
                "object_type_id": src.get("object_type_id") if entity_type == "object" else None,
                "schema_id": src.get("schema_id") if entity_type in ("object", "object_type") else None,
            }
            results.append(result)
        return {"count": total, "results": results}

    def _entity_url(entity_type, src):
        """Build frontend URL for an entity."""
        if entity_type == "object":
            return f"/browse/objects/{src['id']}"
        elif entity_type == "object_type":
            schema_id = src.get("schema_id", "")
            return f"/browse/object-types/{src['id']}" if not schema_id else f"/browse/schemas/{schema_id}"
        elif entity_type == "schema":
            return f"/browse/schemas/{src['id']}"
        return "/"

    return {
        "objects": _parse_hits(responses[0] if len(responses) > 0 else None, "object"),
        "object_types": _parse_hits(responses[1] if len(responses) > 1 else None, "object_type"),
        "schemas": _parse_hits(responses[2] if len(responses) > 2 else None, "schema"),
    }


class _ReindexSnapshot:
    """Execution-local immutable metadata snapshot for one bulk_reindex() call.

    D-11: schema source identity plus eager-loaded OTA/AttributeType/structure
    rows are loaded once per ObjectType rebuild and serve both the index
    mappings and every `_source` document. The snapshot is local to a single
    `bulk_reindex()` invocation — no process-wide or Redis caching and no
    invalidation contract.
    """

    __slots__ = ("source_id", "schema_id", "attrs", "attr_map", "name_map")

    def __init__(
        self,
        source_id: str | None,
        schema_id: uuid.UUID | None,
        attrs,
        name_map: dict[str, str] | None = None,
    ):
        self.source_id = source_id
        self.schema_id = schema_id
        self.attrs = attrs
        self.attr_map = _build_attr_map(attrs)
        self.name_map = name_map


async def _load_reindex_snapshot(db: AsyncSession, object_type_id: uuid.UUID) -> _ReindexSnapshot:
    """Load the immutable schema-source and OTA/AttributeType metadata once
    per ObjectType rebuild (D-12: constant metadata reads regardless of the
    number of objects)."""
    from app.models.object_type import ObjectType

    source_id = None
    schema_id = None
    ot_result = await db.execute(select(ObjectType).where(ObjectType.id == object_type_id))
    ot = ot_result.scalar_one_or_none()
    if ot is not None:
        schema_result = await db.execute(select(Schema).where(Schema.id == ot.schema_id))
        schema = schema_result.scalar_one_or_none()
        source_id = str(schema.source_id) if schema else None
        schema_id = schema.id if schema else None

    attrs_result = await db.execute(
        select(ObjectTypeAttribute)
        .options(selectinload(ObjectTypeAttribute.attribute_type))
        .where(ObjectTypeAttribute.object_type_id == object_type_id)
    )
    attrs = attrs_result.scalars().all()

    # D-15: only pay for the name_map query pair when this ObjectType actually
    # has a USER or GROUP attribute.
    needs_name_map = any(
        attr.attribute_type is not None and attr.attribute_type.data_type in ("user", "group")
        for attr in attrs
    )
    name_map = await _load_user_group_name_map(db) if needs_name_map else None

    return _ReindexSnapshot(source_id, schema_id, attrs, name_map)


# Stable operation label reported for every independent reindex run (D-12).
# Held identical for the creatorless-legacy case (system kind) so a subscriber
# observes the same operation name regardless of whether a creator was persisted.
REINDEX_ORIGIN_NAME = "search.reindex"


async def _load_reindex_attribution(
    db: AsyncSession, task_id: uuid.UUID
) -> tuple[uuid.UUID | None, hook_origin.MutationOrigin]:
    """Resolve the initiating creator of one independent reindex run (D-12/D-13).

    The persisted reindex Task — never the ARQ payload and never the worker
    identity — is the source of truth. A task with a creator yields the explicit
    user reindex shape (``kind="user"``, ``search.reindex``, this run's id); a
    persisted but creatorless legacy task keeps its own run id under the system
    kind without inventing an actor (D-13's null task applies only when no task
    exists at all); a task id that resolves to no row is the genuinely taskless
    system run (``core-index``, null task). One indexed primary-key read.
    """
    from app.models.task import Task

    task = (
        await db.execute(select(Task).where(Task.id == task_id))
    ).scalar_one_or_none()
    if task is None:
        return None, hook_origin.system_origin()
    if task.creator_id is None:
        return None, hook_origin.system_origin(REINDEX_ORIGIN_NAME, task_id=task.id)
    return task.creator_id, hook_origin.user_origin(
        REINDEX_ORIGIN_NAME, task_id=task.id
    )


async def bulk_reindex(db: AsyncSession, object_type_id: uuid.UUID, task_id: uuid.UUID) -> dict:
    # Execution-local metadata snapshot (D-11): schema source identity plus
    # eager-loaded OTA/AttributeType/structure, loaded once per ObjectType
    # rebuild and serving BOTH the fresh mappings and every `_source` document
    # (D-12: metadata reads stay constant as the object count grows).
    snapshot = await _load_reindex_snapshot(db, object_type_id)
    if snapshot.schema_id is None:
        # Stale/nonexistent ObjectType (already deleted, or never existed) —
        # its owning Schema can't be resolved. Skip before any ES client call
        # so no orphan objects_None index is ever created (248-VERIFICATION.md
        # Gap 5 / WR-01).
        return {
            "success": False,
            "object_type_id": str(object_type_id),
            "error": "ObjectType or its Schema not found",
            "total": 0,
            "indexed": 0,
            "errors": 0,
        }

    # D-12/D-13: attribute every hook of this run from the persisted Task, not
    # from the ARQ payload or the worker process. Resolved once per bulk call and
    # reused for both the before_index and after_index dispatches.
    actor_id, origin = await _load_reindex_attribution(db, task_id)
    origin_payload = origin.as_payload()

    es = await get_opensearch()
    index_name = f"objects_{snapshot.schema_id}"

    # Create or extend the schema index, then replace only this ObjectType's
    # documents. Other ObjectTypes share this index and must remain intact.
    await ensure_index(db, snapshot.schema_id, object_type_id)
    await es.delete_by_query(
        index=index_name,
        body={"query": {"term": {"object_type_id": str(object_type_id)}}},
        refresh=True,
        conflicts="proceed",
        ignore=[404],
    )

    # Count total objects
    count_result = await db.execute(
        select(func.count()).select_from(Object).where(Object.object_type_id == object_type_id)
    )
    total = count_result.scalar()

    # Stream objects in pages
    from opensearchpy.helpers import async_bulk

    # PLUGCORE-07: resolve the index-hook subscribers ONCE before the paging
    # loop. When nothing subscribes the loop body below is untouched — bulk
    # reindex performance for the no-plugin case does not change.
    hook_registry = _hook_registry()
    before_index_subs = (
        plugin_hooks.plugins_subscribed_to(hook_registry, "before_index")
        if hook_registry is not None
        else []
    )
    after_index_subs = (
        plugin_hooks.plugins_subscribed_to(hook_registry, "after_index")
        if hook_registry is not None
        else []
    )
    after_index_objects: list[tuple[uuid.UUID, uuid.UUID]] = []

    actions = []
    offset = 0
    page_size = 100
    while True:
        result = await db.execute(
            select(Object)
            .options(
                selectinload(Object.values),
            )
            .where(Object.object_type_id == object_type_id)
            .offset(offset).limit(page_size)
        )
        objects = result.unique().scalars().all()
        if not objects:
            break
        for obj in objects:
            doc = _build_document_from_attr_map(
                obj, snapshot.source_id, snapshot.attr_map, snapshot.name_map
            )
            actions.append({"_index": index_name, "_id": str(obj.id), "_source": doc})
            if before_index_subs:
                await plugin_hooks.dispatch_hook(
                    hook_registry,
                    "before_index",
                    before_index_subs,
                    active_session=db,
                    object_id=obj.id,
                    schema_id=snapshot.schema_id,
                    document=doc,
                    actor_id=actor_id,
                    origin=origin_payload,
                )
            if after_index_subs:
                after_index_objects.append((obj.id, snapshot.schema_id))
        offset += page_size

    success_count, error_count = 0, 0
    if actions:
        # stats_only=True — without it async_bulk returns a *list* of error dicts as the
        # second value instead of a count, breaking reindex_schema's `total_errors += ...`.
        success_count, error_count = await async_bulk(es, actions, chunk_size=500, stats_only=True)

    # after_index only once the write actually succeeded (error_count == 0).
    if error_count == 0 and after_index_subs:
        for obj_id, schema_id in after_index_objects:
            await plugin_hooks.dispatch_hook(
                hook_registry,
                "after_index",
                after_index_subs,
                active_session=db,
                object_id=obj_id,
                schema_id=schema_id,
                actor_id=actor_id,
                origin=origin_payload,
            )
    return {
        "success": error_count == 0,
        "object_type_id": str(object_type_id),
        "total": total,
        "indexed": success_count,
        "errors": error_count,
    }


async def reindex_schema(db: AsyncSession, schema_id: uuid.UUID, task_id: uuid.UUID) -> dict:
    """Reindex all ObjectTypes under a Schema.

    Rebuilds the Schema's static catalog document (D-07 schemas index), then
    iterates all ObjectTypes for the schema calling index_object_type and
    bulk_reindex for each. Returns aggregated results.
    """
    from app.models.object_type import ObjectType
    from sqlalchemy import select

    # Load all ObjectTypes for this schema
    result = await db.execute(
        select(ObjectType).where(ObjectType.schema_id == schema_id)
    )
    object_types = result.scalars().all()

    # Carry-03: a safe, generic Core message for a reindex failure. The raw
    # per-type error string or exception text is never stored or logged.
    from app.services.task_service import safe_error_text

    reindex_generic = "The reindex failed. See server logs for details."

    total_objects = 0
    total_indexed = 0
    total_errors = 0
    processed_count = 0
    errors_list = []

    # D-07 catalog legs: the static catalog indices are separate writes from
    # the per-Schema objects_* index. A full reindex rebuilds the Schema's
    # catalog document too — previously the catalog docs were only written on
    # entity create/update, so a search-backend swap left a reindexed schema
    # absent from the schemas index until its next edit. A catalog failure is
    # collected like a per-type failure: it must never abort the job.
    try:
        schema_catalog_result = await index_schema(schema_id)
        if not schema_catalog_result.get("success", True):
            error_msg = safe_error_text(None, reindex_generic)
            logger.error("%s", error_msg)
            errors_list.append(error_msg)
            total_errors += 1
    except Exception as e:
        error_msg = safe_error_text(e, reindex_generic)
        logger.error("%s", error_msg)
        errors_list.append(error_msg)
        total_errors += 1

    for ot in object_types:
        try:
            per_type_result = await bulk_reindex(db, ot.id, task_id)
            total_objects += per_type_result.get("total", 0)
            total_indexed += per_type_result.get("indexed", 0)
            total_errors += per_type_result.get("errors", 0)

            # D-07 catalog legs: rebuild this ObjectType's static catalog
            # document alongside its objects_* documents, collected the same
            # way as a per-type failure.
            object_type_catalog_result = await index_object_type(ot.id)
            if not object_type_catalog_result.get("success", True):
                error_msg = safe_error_text(None, reindex_generic)
                logger.error("%s", error_msg)
                errors_list.append(error_msg)
                total_errors += 1

            if per_type_result.get("success", True):
                processed_count += 1
            else:
                # Do not adopt the unchecked ``error`` string from the result.
                error_msg = safe_error_text(None, reindex_generic)
                logger.error("%s", error_msg)
                errors_list.append(error_msg)
        except Exception as e:
            # Project the exception to its type plus the generic message BEFORE
            # any string formatting, so no raw exception text can leak.
            error_msg = safe_error_text(e, reindex_generic)
            logger.error("%s", error_msg)
            errors_list.append(error_msg)
            total_errors += 1

    return {
        "success": len(errors_list) == 0,
        "schema_id": str(schema_id),
        "object_types_processed": processed_count,
        "total": total_objects,
        "indexed": total_indexed,
        "errors": total_errors,
        "error_messages": errors_list,
    }
