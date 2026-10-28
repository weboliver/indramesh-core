import hashlib
import json
import logging
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, List

from sqlalchemy import select, func, update
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Object, Schema, ObjectType, ObjectValue, ObjectTypeAttribute, AttributeType, AttributeTypeDataType
from app.models.index_outbox import IndexOutbox, IndexOutboxStatus
from app.config import storage_settings
from app.services.storage_client import get_storage_client
from app.services.filter_utils import build_filter_clause, _get_value_column, resolve_user_group_literal
from app.services.select_option_service import (
    build_option_map, resolve_display_value, resolve_select_literal,
)
from app.schemas.crud import ObjectCreate, ObjectUpdate, ObjectResponse
from app.schemas.classification import CLASSIFICATION_INPUT_FIELDS
from app.services.classification_service import (
    attach_classifications,
    classification_audit_keys,
    classification_kwargs,
    set_labels,
)
from app.services.translation_service import resolve_translations, delete_translations_for
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
from app.services.record_permission_service import build_record_permission_filter, _batch_check_has_deny_record_permission, _batch_check_has_record_permission
from app.services.object_type_service import get_descendant_type_ids, resolve_ancestor_ota_by_name
from app.services.value_lock_service import (
    assert_types_deletable,
    assert_value_writable,
    resolve_name_attribute,
)
from app.services.keygen_service import next_key
from app.db.redis import get_arq_pool
from app.services.audit_service import audit_log, model_to_dict
from app.services import hook_origin
from app.plugin_system import get_plugin_registry
from app.plugin_system import hooks as plugin_hooks


logger = logging.getLogger(__name__)


def _resolve_origin(
    origin: hook_origin.MutationOrigin | None, operation: str, actor_id: uuid.UUID | None
) -> hook_origin.MutationOrigin:
    """The trusted hook origin for one Object mutation (D-14).

    An explicit caller-supplied origin wins (SDK plugin writes, CSV import).
    Otherwise an authenticated actor yields a user origin named after the stable
    service operation, and a null actor (internal/system paths) yields the
    system origin — never a synthetic user identity.
    """
    if origin is not None:
        return origin
    if actor_id is not None:
        return hook_origin.user_origin(operation)
    return hook_origin.system_origin()


async def _fire_object_hook(hook_name: str, db: AsyncSession, **kwargs: Any) -> None:
    """Fire an object-mutation hook (D-04/PLUGCORE-07) best-effort.

    Hooks are additive observers: an uninitialized registry (unit tests calling
    these service functions without the app lifespan) must never break core
    CRUD, so a ``RuntimeError`` from ``get_plugin_registry()`` skips hook-firing
    silently. The caller's own ``db`` is passed through as the active session
    (D-06 — the hook shares the running transaction).
    """
    try:
        registry = get_plugin_registry()
    except RuntimeError:
        return
    await plugin_hooks.fire_hook(registry, hook_name, active_session=db, **kwargs)


def _new_outbox_entry(
    object_id: uuid.UUID,
    object_type_id: uuid.UUID | None,
    operation: str,
    schema_id: uuid.UUID | None = None,
    *,
    actor_id: uuid.UUID | None = None,
    origin: hook_origin.MutationOrigin | None = None,
) -> IndexOutbox:
    """Build an outbox row with an explicit id so it can be referenced before flush.

    CORESEAM-11/D-11: the mutation actor and origin are staged on the row in the
    SAME transaction as the Object/value write, so the worker never has to
    guess provenance from the ARQ payload or the worker user. ``origin=None``
    stores all four columns NULL — the worker normalizes such a (legacy or
    genuinely actorless) row to the explicit system shape, so this helper never
    fabricates a user/plugin identity.
    """
    return IndexOutbox(
        id=uuid.uuid4(),
        object_id=object_id,
        object_type_id=object_type_id,
        schema_id=schema_id,
        operation=operation,
        actor_id=actor_id,
        origin_kind=origin.kind if origin is not None else None,
        origin_name=origin.name if origin is not None else None,
        origin_task_id=origin.task_id if origin is not None else None,
    )


def _resolve_select_display_loaded(obj: Object) -> Object:
    """Resolve SELECT option ids on a detached Object response copy.

    The object read paths (get_object/list_objects) do NOT eager-load values
    today, and unloaded relationships are never touched (the vars() guard
    prevents any lazy load — no MissingGreenletError in async context), so
    this is a no-op for them. It guards any future path that hands in an
    Object with values (and their attributes) already loaded.
    """
    if "values" not in vars(obj):
        return obj
    obj = deepcopy(obj)
    option_maps: dict[uuid.UUID, dict[str, str]] = {}
    for v in obj.values:
        if "object_type_attribute" not in vars(v) or v.object_type_attribute is None:
            continue
        attr = v.object_type_attribute
        if attr.id not in option_maps:
            option_maps[attr.id] = build_option_map(attr)
        option_map = option_maps[attr.id]
        if option_map and v.value_simple:
            v.value_simple = resolve_display_value(v.value_simple, option_map)
    return obj


async def _resolve_names_in_place_lang(
    db: AsyncSession, items: list[Object], lang: str,
) -> dict[uuid.UUID, tuple[str, str]]:
    """V-06: resolve Object.name via the Name attribute's value translation.

    ``Object.name`` is the denormalized copy of the "Name" attribute's value
    (synced by object_value_service._sync_object_name). The Name attribute is
    identified by string match on its name — the ONLY existing convention
    (there is no name_attribute_id FK; do NOT confuse with the unrelated
    ``label_attribute_id``).

    Chain (CONTEXT V-06): requested -> schema default -> base. Only values a
    translation row actually served are returned: {object_id: (resolved_name,
    serving_code)}; the caller serves the base name for the rest and sets
    ``resolved_language`` from the NAME's serving language only (V-08).

    Batched shape (T-224-04): object types -> Name attributes -> values ->
    ``resolve_translations`` — never per-object queries. Eligibility gates
    (attribute+parent multilingual flags, TEXT/SELECT/STATUS dtype) live
    inside ``resolve_translations`` (V-05) — dormant values yield no entries
    and the caller keeps the base name.
    """
    if not items:
        return {}
    ot_ids = {item.object_type_id for item in items}
    attr_result = await db.execute(
        select(ObjectTypeAttribute).where(
            ObjectTypeAttribute.object_type_id.in_(ot_ids),
            ObjectTypeAttribute.name == "Name",
        )
    )
    attrs_by_ot: dict[uuid.UUID, list[ObjectTypeAttribute]] = {}
    for attr in attr_result.scalars().all():
        attrs_by_ot.setdefault(attr.object_type_id, []).append(attr)
    attr_ids = [a.id for attrs in attrs_by_ot.values() for a in attrs]
    if not attr_ids:
        return {}

    object_ids = [item.id for item in items]
    # Most-recent-write-wins (Phase 253 mirror-on-create can leave an earlier,
    # never-translated Name value alongside a later explicit one): Object.name
    # itself always reflects the latest write (_sync_object_name), so the
    # translation lookup must key off the same value or it resolves against
    # a value nobody displays.
    value_result = await db.execute(
        select(ObjectValue.id, ObjectValue.object_id)
        .where(
            ObjectValue.object_type_attribute_id.in_(attr_ids),
            ObjectValue.object_id.in_(object_ids),
        )
        .order_by(ObjectValue.created_at.desc())
    )
    value_by_object: dict[uuid.UUID, uuid.UUID] = {}
    for value_id, object_id in value_result.all():
        value_by_object.setdefault(object_id, value_id)
    if not value_by_object:
        return {}

    resolved = await resolve_translations(
        db, "object_value", list(value_by_object.values()), ["value"],
        lang, schema_id=None,
    )
    out: dict[uuid.UUID, tuple[str, str]] = {}
    for object_id, value_id in value_by_object.items():
        entry = resolved.get(value_id, {}).get("value")
        if entry:
            out[object_id] = entry
    return out


async def _enqueue_index_job(
    db: AsyncSession, object_id: uuid.UUID, job_name: str = "index_object",
    outbox_id: uuid.UUID | None = None, *, persist_status: bool = True,
    **extra_kwargs: str
) -> None:
    """Best-effort enqueue of an ARQ search indexing job.

    Non-blocking — failures are logged, not raised. The matching IndexOutbox row
    (written in the same transaction as the Object mutation) is the real
    source of truth: if enqueue fails here (e.g. Redis down), the row stays
    PENDING/ENQUEUED and outbox_sweeper_job picks it up later, so the index
    update is never silently lost.

    ``persist_status=False`` is the composite-writer mode (D-15, one unit of
    work per object): the caller already wrote its single outbox row with
    ``status=ENQUEUED`` INSIDE the data transaction, so this helper must not
    issue a status UPDATE — that UPDATE plus its own commit would turn a
    one-commit composite write into two. The row still needs no extra safety:
    ``outbox_sweeper_job`` reaps rows whose status is PENDING **or** ENQUEUED
    once the two-minute cooldown has passed, so a failed enqueue on this path
    is retried exactly like a failed one on the default path.

    The ARQ _job_id includes outbox_id, not just object_id. ARQ's enqueue_job
    treats an existing *result* key as a duplicate too, not only a queued/
    in-progress one — and WorkerSettings.keep_result=86400 keeps that result
    key alive for 24h. A job_id scoped to object_id alone therefore silently
    dropped every later index request for the same object within 24h of the
    first (enqueue_job returns None, mistaken below for "another in-flight
    job already covers this"), even though that earlier job had long since
    completed against stale data. outbox_id is fresh per call/outbox row, so
    every real mutation gets its own job; only a genuine re-enqueue of the
    *same* outbox row still collapses — and that collapse no longer claims
    DONE, because only the job's own result may (EXT-06-T1).
    """
    try:
        arq_pool = await get_arq_pool()
        job_id = f"{job_name}_{object_id}_{outbox_id}" if outbox_id else f"{job_name}_{object_id}"
        await arq_pool.enqueue_job(
            job_name,
            str(object_id),
            **extra_kwargs,
            outbox_id=str(outbox_id) if outbox_id else None,
            _job_id=job_id,
        )
        if outbox_id is not None and persist_status:
            # ENQUEUED regardless of enqueue_job's return value. A None here only
            # means ARQ already holds this exact job id (same outbox row) — the
            # job exists, but that is not proof the index write happened either.
            # DONE is only ever written by the job's own result (EXT-06-T1); a
            # stale/duplicate entry is reaped by outbox_sweeper_job instead.
            await db.execute(
                update(IndexOutbox)
                .where(IndexOutbox.id == outbox_id)
                .values(status=IndexOutboxStatus.ENQUEUED.value)
            )
            await db.commit()
    except Exception:
        logger.exception("failed to enqueue %s job for object %s", job_name, object_id)


async def _touch_system_attribute(
    db: AsyncSession, object_id: uuid.UUID, object_type_id: uuid.UUID, attr_name: str,
    *, commit: bool = True,
) -> None:
    """Upsert a hidden system timestamp attribute ('Created'/'Updated') for an object.

    Writes value_datetime directly against the ObjectValue table rather than
    routing through create_object_value/update_object_value, so this cannot
    recursively re-trigger itself. Silently no-ops if the ObjectType has no
    such attribute (e.g. an ObjectType not provisioned with the native system
    attributes has no virtual Created/Updated ObjectTypeAttribute).

    ``commit=False`` is the composite-writer mode (D-15): both statements keep
    running, but the transaction boundary belongs to the caller, so each
    ``commit()`` becomes a ``flush()``.

    For "Updated", this also bumps Object.updated_at itself so a value/ref
    write that leaves the Object row untouched still records that the Object
    changed — editing an ObjectValue/ObjectRef never touches the Object row on
    its own (SQLAlchemy's onupdate= only fires when a mapped column of Object
    itself is part of the flush), so without this the change would not be
    observable from the Object row.
    """
    if attr_name == "Updated":
        await db.execute(
            update(Object).where(Object.id == object_id).values(updated_at=datetime.now(timezone.utc).replace(tzinfo=None))
        )
        if commit:
            await db.commit()
        else:
            await db.flush()

    result = await db.execute(
        select(ObjectTypeAttribute).where(
            ObjectTypeAttribute.object_type_id == object_type_id,
            ObjectTypeAttribute.name == attr_name,
            ObjectTypeAttribute.is_system.is_(True),
        )
    )
    attribute = result.scalar_one_or_none()
    if not attribute:
        # 250-Korrektur: kein eigenes System-OTA (z.B. weil das Kind von einem
        # vererbenden Parent übernimmt und bei der Anlage kein eigenes Created/
        # Updated provisioniert wurde) — in das geerbte Parent-OTA referenzieren.
        inherited_id = await resolve_ancestor_ota_by_name(db, attr_name, object_type_id)
        if not inherited_id:
            return
        attribute = await db.get(ObjectTypeAttribute, inherited_id)
    if not attribute:
        return

    result = await db.execute(
        select(ObjectValue).where(
            ObjectValue.object_id == object_id,
            ObjectValue.object_type_attribute_id == attribute.id,
        )
    )
    value = result.scalar_one_or_none()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if value:
        value.value_datetime = now
        value.value_simple = None
    else:
        db.add(ObjectValue(
            object_id=object_id,
            object_type_attribute_id=attribute.id,
            value_datetime=now,
        ))
    if commit:
        await db.commit()
    else:
        await db.flush()


async def _resolve_system_ota(
    db: AsyncSession, object_type_id: uuid.UUID, attr_name: str
) -> ObjectTypeAttribute | None:
    """Own-type system OTA first, inherited (250 Weg A) parent OTA as fallback.

    Same resolution order as _touch_system_attribute — a child ObjectType whose
    system attributes live in the defining parent must write into the parent's
    OTA rather than provisioning one of its own.
    """
    result = await db.execute(
        select(ObjectTypeAttribute).where(
            ObjectTypeAttribute.object_type_id == object_type_id,
            ObjectTypeAttribute.name == attr_name,
            ObjectTypeAttribute.is_system.is_(True),
        )
    )
    attribute = result.scalar_one_or_none()
    if attribute:
        return attribute
    inherited_id = await resolve_ancestor_ota_by_name(db, attr_name, object_type_id)
    if not inherited_id:
        return None
    return await db.get(ObjectTypeAttribute, inherited_id)


async def _mirror_system_value(
    db: AsyncSession, object_id: uuid.UUID, object_type_id: uuid.UUID,
    attr_name: str, value_simple: str | None,
) -> None:
    """253 D-01: mirror an Object column into a real ObjectValue row under the
    matching system ObjectTypeAttribute.

    The Object column stays the source of truth; this row is the queryable
    mirror the read path and the detail panel render. Deliberately does NOT
    commit — the caller owns the transaction so the mirror is atomic with the
    Object insert. No-ops when the ObjectType has no such system attribute
    (pre-246 types).
    """
    attribute = await _resolve_system_ota(db, object_type_id, attr_name)
    if not attribute:
        return

    result = await db.execute(
        select(ObjectValue).where(
            ObjectValue.object_id == object_id,
            ObjectValue.object_type_attribute_id == attribute.id,
        )
    )
    value = result.scalar_one_or_none()
    if value:
        value.value_simple = value_simple
    else:
        db.add(ObjectValue(
            object_id=object_id,
            object_type_attribute_id=attribute.id,
            value_simple=value_simple,
        ))


async def create_object(
    db: AsyncSession,
    object_data: ObjectCreate,
    actor_id: uuid.UUID,
    *,
    origin: hook_origin.MutationOrigin | None = None,
    id: uuid.UUID | None = None,
    commit: bool = True,
    emit_index_outbox: bool = True,
    invalidate_cache: bool = True,
) -> Object | None:
    """Create one Object as one unit of work (D-15).

    ``commit=False`` / ``emit_index_outbox=False`` / ``invalidate_cache=False``
    are the composite-writer switches (the public create/replace routes): the
    caller owns the single transaction, writes exactly one object-level
    IndexOutbox row itself, enqueues it after its own commit and purges the
    cache once. Defaults keep every existing caller (REST, SDK, CSV import)
    byte-for-byte unchanged.
    """
    result = await db.execute(
        select(Schema).where(Schema.id == object_data.schema_id)
    )
    schema = result.scalar_one_or_none()
    if not schema:
        return None

    result = await db.execute(
        select(ObjectType).where(ObjectType.id == object_data.object_type_id)
    )
    object_type = result.scalar_one_or_none()
    if not object_type:
        return None
    if object_type.schema_id != object_data.schema_id:
        return None
    if object_type.is_abstract:
        # INH-02 (D-02/D-03): abstract ObjectTypes never normally receive
        # Objects — independent of inherits_attributes (a type may be both
        # abstract AND an inheriting parent). Raise so the router can surface
        # a precise 400 instead of the generic 404 "Schema or object type
        # not found".
        raise ValueError("Cannot create an Object under an abstract ObjectType")

    # 290 D-02/D-05: a REST create whose user-supplied name will actually be
    # mirrored into a persisted Name ObjectValue must not bypass the effective
    # lock (REST create is native by contract, plan 290-10). A type whose label
    # is a foreign attribute never mirrors Name, so it stays creatable while
    # locked; server-generated Key/Created/Updated values stay writable (D-03).
    label_ota = (
        await db.get(ObjectTypeAttribute, object_type.label_attribute_id)
        if object_type.label_attribute_id else None
    )
    if label_ota is None or label_ota.name == "Name":
        name_ota = await _resolve_system_ota(db, object_type.id, "Name")
        if name_ota is not None:
            await assert_value_writable(
                db,
                object_type_id=object_type.id,
                attribute_id=name_ota.id,
                origin=origin,
            )

    db_object = Object(
        id=id or uuid.uuid4(),
        schema_id=object_data.schema_id,
        object_type_id=object_data.object_type_id,
        name=object_data.name,
    )
    # 246 OBJ-02 (D-10/D-11): the REST create route always generates the
    # native schema key. Keyless rows only ever come from the CSV import
    # direct-ORM path.
    db_object.key = await next_key(db, schema)
    db.add(db_object)
    # CORESEAM-11/D-11: resolve the trusted origin before the outbox row so the
    # index job carries the initiator through later retries.
    effective_origin = _resolve_origin(origin, "object.create", actor_id)
    outbox_entry = None
    if emit_index_outbox:
        outbox_entry = _new_outbox_entry(
            db_object.id, db_object.object_type_id, "index",
            actor_id=actor_id, origin=effective_origin,
        )
        db.add(outbox_entry)
    await db.flush()
    # D-07: a create carries all three families; an all-default payload writes
    # no assignment row at all (the composite public create path never sends a
    # classification field — D-17 — so it stays a pure object insert).
    created_classification = classification_kwargs(object_data, partial=False)
    created_label_refs = None
    if (
        created_classification["confidentiality"] is not None
        or created_classification["personal_data"]
        or created_classification["export_control"]
    ):
        # A ValueError (unknown / wrong-family label id) reaches the route as
        # 400 and the flushed Object row is never committed.
        _, created_label_refs = await set_labels(
            db, "object", db_object.id, **created_classification
        )
    new_values = model_to_dict(db_object)
    if created_label_refs is not None:
        new_values["classification_labels"] = classification_audit_keys(created_label_refs)
    await audit_log(db, "objects", db_object.id, "CREATE", actor_id,
                    old_values=None, new_values=new_values, object_id=db_object.id)
    # PLUGCORE-07: fire the object-created hook in the same transaction as the
    # mutation (D-06), immediately after audit_log. CORESEAM-11/D-14: the payload
    # carries the trusted actor and origin plus the always-present
    # attribute-change keys (empty here — plans 02/03 fill them for value writes).
    await _fire_object_hook(
        "on_object_created",
        db,
        object_id=db_object.id,
        schema_id=db_object.schema_id,
        object_type_id=db_object.object_type_id,
        new_values=new_values,
        actor_id=actor_id,
        origin=effective_origin.as_payload(),
        changed_attribute_ids=[],
        attribute_changes=[],
    )
    # 253 D-01/D-02/D-04: mirror the Object columns into real ObjectValue rows
    # for the auto-provisioned system attributes, in the same transaction as the
    # Object insert. Name is written only when it is the ObjectType's label
    # (D-02) — a type whose
    # label is a foreign attribute (e.g. "IP4") has no required Name field and
    # must not get one auto-filled. "Created" keeps its own path below;
    # "Updated" stays empty until the first edit (D-03).
    if db_object.key is not None:
        await _mirror_system_value(
            db, db_object.id, db_object.object_type_id, "Key", db_object.key
        )
    if label_ota is None or label_ota.name == "Name":
        await _mirror_system_value(
            db, db_object.id, db_object.object_type_id, "Name", db_object.name
        )
    # CR314-27: the Object row and its "Created" system value are ONE unit of
    # work. Writing the timestamp after the commit left a persisted Object
    # behind whenever that second write failed, so the caller answered 500 for
    # an object that already existed. Both now ride the single commit below —
    # on the default path as well as the composite one.
    await _touch_system_attribute(
        db, db_object.id, db_object.object_type_id, "Created", commit=False
    )
    # D-07: attach before the branch, so the composite (``commit=False``) path
    # returns the same classified shape as the committed one instead of an
    # instance whose rows are written but whose labels read as empty. The rows
    # are already flushed by set_labels, so this is ordering only.
    if created_label_refs is not None:
        await attach_classifications(db, "object", [db_object])
    if commit:
        await db.commit()
        await db.refresh(db_object)
    else:
        await db.flush()
        # The composite caller owns the enqueue and the cache purge (D-15) —
        # it holds the one object-level outbox row this write must share.
        return db_object

    if outbox_entry is not None:
        await _enqueue_index_job(db, db_object.id, "index_object_job", outbox_id=outbox_entry.id)
    if invalidate_cache:
        await invalidate_entity_cache("object")
        await invalidate_entity_cache("search")
    return db_object


# _get_value_column is imported from filter_utils to avoid circular import

# sort_by → Object column. "name" stays the default for any unknown/omitted
# value — matches the pre-existing behavior from before "created"/"updated"
# were added, so old callers passing sort_by=None keep sorting by name.
_SORT_COLUMNS = {
    "name": Object.name,
    "created": Object.created_at,
    "updated": Object.updated_at,
}


async def list_objects(
    db: AsyncSession, skip: int = 0, limit: int = 100,
    object_type_id: uuid.UUID | None = None,
    object_type_ids: list[uuid.UUID] | None = None,  # AQL-09: objectType IN (...)
    schema_id: uuid.UUID | None = None,
    q: str | None = None,
    attr_filters: dict[str, str] | None = None,
    filter_tree: dict | None = None,
    pk_filter: list[uuid.UUID] | None = None,   # NEW — per D-03 (AQL hybrid ES→SQL handoff)
    sort_by: str | None = None,
    sort_order: str | None = None,
    user_role: str | None = None,  # NEW per D-11
    user_id: str | None = None,
    is_superuser: bool = False,
    lang: str | None = None,  # V-06 name resolution (224-04)
) -> tuple[List[Object], int]:
    query = select(Object)
    count_query = select(func.count()).select_from(Object)
    if schema_id:
        # D-182-P-07 Option 1: filter Objects whose ObjectType belongs to the
        # given Schema via a subquery. Combined with object_type_id it
        # intersects both filters (narrower scope).
        query = query.where(
            Object.object_type_id.in_(
                select(ObjectType.id).where(ObjectType.schema_id == schema_id)
            )
        )
        count_query = count_query.where(
            Object.object_type_id.in_(
                select(ObjectType.id).where(ObjectType.schema_id == schema_id)
            )
        )
    if object_type_ids:
        # AQL-09: objectType IN (...) — exact-types-only, no descendant-type
        # expansion (an explicit multi-type list already states the intent;
        # mixing it with hierarchy expansion would be ambiguous).
        query = query.where(Object.object_type_id.in_(object_type_ids))
        count_query = count_query.where(Object.object_type_id.in_(object_type_ids))
    elif object_type_id:
        # D-01: exact-type-only scoping is the base rule.
        query = query.where(Object.object_type_id == object_type_id)
        count_query = count_query.where(Object.object_type_id == object_type_id)
        # D-02/D-03: if the requested type has descendants, exclude them explicitly
        # via a cycle-safe recursive CTE (mirrors object_type_service.is_ancestor).
        # The EXISTS pre-check inside get_descendant_type_ids makes leaf types
        # skip the CTE entirely (early-exit), so this is cheap for the common case.
        descendant_ids = await get_descendant_type_ids(db, object_type_id)
        if descendant_ids:
            query = query.where(Object.object_type_id.notin_(descendant_ids))
            count_query = count_query.where(Object.object_type_id.notin_(descendant_ids))
    if q:
        query = query.where(Object.name.ilike(f"%{q}%"))
        count_query = count_query.where(Object.name.ilike(f"%{q}%"))
    if filter_tree:
        from app.services.filter_utils import evaluate_filter_tree
        tree_clause = await evaluate_filter_tree(filter_tree, db)
        if tree_clause is not None:
            query = query.where(tree_clause)
            count_query = count_query.where(tree_clause)
    elif attr_filters:
        # Pre-fetch phase: collect all unique attr_ids and batch-load OTAs with AttributeTypes
        # This eliminates the N+1 pattern that previously ran per attr_id in the loop below
        unique_attr_ids = set()
        for attr_id_str in attr_filters.keys():
            try:
                unique_attr_ids.add(uuid.UUID(attr_id_str))
            except ValueError:
                pass

        # Batch-load all OTAs with their AttributeTypes in one query
        attr_lookup = {}  # attr_id → (ObjectTypeAttribute, AttributeType)
        if unique_attr_ids:
            result = await db.execute(
                select(ObjectTypeAttribute)
                .options(selectinload(ObjectTypeAttribute.attribute_type))
                .where(ObjectTypeAttribute.id.in_(unique_attr_ids))
            )
            for attr in result.scalars():
                attr_lookup[attr.id] = (attr, attr.attribute_type)

        for attr_id_str, filter_val in attr_filters.items():
            try:
                attr_id = uuid.UUID(attr_id_str)
            except ValueError:
                continue

            # Lookup attribute and type from pre-fetched dict (no new query)
            if attr_id not in attr_lookup:
                continue
            attr, at = attr_lookup[attr_id]
            dtype = at.data_type if at else None
            value_col = _get_value_column(dtype)
            # Detect structured operator format vs flat value
            if isinstance(filter_val, dict):
                # Structured operator format: {op: "gt", val: 10, val2: optional}
                # build_filter_clause raises ValueError for invalid operator/dtype
                # combinations — let it propagate uncaught; objects.py's route handler
                # catches it and converts to HTTP 422 (per D-15). Do NOT catch it here.
                if dtype == "select" and filter_val.get("val") is not None:
                    # D-05: SELECT literals are display strings — resolve to the
                    # option id before the SQL comparison (ids stored in value_simple).
                    filter_val = {
                        **filter_val,
                        "val": resolve_select_literal(
                            attr.structure, str(filter_val["val"]), attr_name=attr.name
                        ),
                    }
                elif dtype in ("user", "group") and filter_val.get("val") is not None:
                    # D-15: user/group filter literals resolve names to UUIDs
                    # before the SQL comparison, with raw pass-through (D-07)
                    # for legacy name-stored GROUP values.
                    filter_val = {
                        **filter_val,
                        "val": await resolve_user_group_literal(
                            db, dtype, str(filter_val["val"])
                        ),
                    }
                subq = build_filter_clause(attr_id, filter_val, dtype, attr_name=attr.name)
            else:
                # Backward-compatible flat format: {attr_id: "value"} → eq
                # Preserve existing coercion logic for flat format
                coerced_val: Any = filter_val
                if dtype == AttributeTypeDataType.JSON.value:
                    continue  # JSON column filtering not supported
                elif dtype == AttributeTypeDataType.STATUS.value:
                    # D-08/D-09: _get_value_column("status") now correctly
                    # resolves to value_struct (a JSON column) via the seam —
                    # this flat/backward-compatible branch does a plain
                    # `value_col == coerced_val` equality below with no
                    # JSON-aware handling (unlike build_filter_clause's new
                    # dedicated _status_filter), so a raw string here would
                    # be a Postgres "operator does not exist: json = text"
                    # error. Flat-format STATUS filtering was already
                    # effectively unsupported before this plan (value_simple
                    # is never populated for STATUS, so the old comparison
                    # silently matched nothing) — skip explicitly instead of
                    # newly crashing, matching the JSON/datetime pattern above.
                    continue
                elif dtype == AttributeTypeDataType.BOOLEAN.value:
                    coerced_val = str(filter_val).lower() in ("true", "1", "yes")
                elif dtype == AttributeTypeDataType.INTEGER.value:
                    try:
                        coerced_val = int(filter_val)
                    except (ValueError, TypeError):
                        continue
                elif dtype == AttributeTypeDataType.DOUBLE.value:
                    try:
                        coerced_val = float(filter_val)
                    except (ValueError, TypeError):
                        continue
                elif dtype == AttributeTypeDataType.DATETIME.value:
                    continue  # flat datetime strings are ambiguous — skip as before
                elif dtype == AttributeTypeDataType.SELECT.value:
                    # D-05: flat-format display string -> option id (same
                    # resolution as the structured branch above).
                    coerced_val = resolve_select_literal(
                        attr.structure, str(filter_val), attr_name=attr.name
                    )
                elif dtype in (AttributeTypeDataType.USER.value, AttributeTypeDataType.GROUP.value):
                    # D-15: flat-format user/group literal -> UUID resolution
                    # (same resolution as the structured branch above).
                    coerced_val = await resolve_user_group_literal(
                        db, dtype, str(filter_val)
                    )
                subq = (
                    select(ObjectValue.object_id)
                    .where(
                        ObjectValue.object_type_attribute_id == attr_id,
                        value_col == coerced_val,
                    )
                )
            query = query.where(Object.id.in_(subq))
            count_query = count_query.where(Object.id.in_(subq))
    # pk_filter: ES-derived PK constraint (AQL hybrid path, per D-03)
    # Applied as bound array parameter, NOT literal IN list.
    if pk_filter is not None:
        query = query.where(Object.id.in_(pk_filter))
        count_query = count_query.where(Object.id.in_(pk_filter))
    if user_id is not None:
        filter_clause = await build_record_permission_filter(
            db, user_id, "object", is_superuser=is_superuser
        )
        if filter_clause is not None:
            query = query.where(filter_clause)
            count_query = count_query.where(filter_clause)
    sort_column = _SORT_COLUMNS.get(sort_by, Object.name)
    if sort_order == "desc":
        query = query.order_by(sort_column.desc())
    else:
        query = query.order_by(sort_column.asc())
    filter_tree_json = json.dumps(filter_tree, sort_keys=True) if filter_tree else None
    pk_filter_hash = None
    if pk_filter is not None:
        pk_filter_hash = hashlib.md5(
            str(sorted([str(pid) for pid in pk_filter])).encode()
        ).hexdigest()
    object_type_ids_key = (
        ",".join(sorted(str(t) for t in object_type_ids)) if object_type_ids else None
    )
    cache_key = build_list_cache_key(
        "object", skip=skip, limit=limit,
        object_type_id=object_type_id, object_type_ids=object_type_ids_key,
        schema_id=schema_id, q=q,
        attr_filters=json.dumps(attr_filters, sort_keys=True) if attr_filters and not filter_tree else None,
        filter_tree=filter_tree_json,
        pk_filter=pk_filter_hash,  # NEW — separates ES-narrowed from unnarrowed cache keys
        sort_by=sort_by,
        sort_order=sort_order,
        user_role=user_role,  # NEW per D-11
        user_id=user_id,
        lang=lang,  # 224-04: lang partitions the list cache per language (MULTI-06 pattern)
    )
    cached = await get_cached(cache_key)
    if cached is not None:
        cached_items = cached.get("items") if isinstance(cached, dict) else None
        if isinstance(cached_items, list) and all(
            isinstance(item, dict) and isinstance(item.get("classification_labels"), list)
            for item in cached_items
        ):
            return cached["items"], cached["total"]
    count_result = await db.execute(count_query)
    total = count_result.scalar()
    result = await db.execute(query.offset(skip).limit(limit))
    items = result.scalars().all()

    deny_ids: set[str] = set()
    any_perm_ids: set[str] = set()
    if items:
        deny_ids = await _batch_check_has_deny_record_permission(
            db, [item.id for item in items], "object",
        )
        any_perm_ids = await _batch_check_has_record_permission(
            db, [item.id for item in items], "object",
        )
    for item in items:
        item.has_deny_record_permission = str(item.id) in deny_ids
        item.has_record_permission = str(item.id) in any_perm_ids

    # T-317.1-24: one batch read attaches the labels to the ORM rows before
    # both serializations and cache writes, so a list payload can never report
    # an object as unclassified while it carries labels.
    await attach_classifications(db, "object", list(items))

    # T-223-18: never cache an unresolved payload — defensive resolution for
    # any loaded ObjectValue rows (no-op today: values aren't eager-loaded).
    items = [_resolve_select_display_loaded(item) for item in items]

    if lang:
        # 224-04: V-06 batched name resolution — resolve BEFORE the cache set
        # so a lang-scoped slot always holds the resolved payload. Serialized
        # via the response model (AGENTS.md hard rule — resolved_language is
        # computed and must survive the cache round-trip).
        resolved_names = await _resolve_names_in_place_lang(db, list(items), lang)
        serialized_items = [
            ObjectResponse.model_validate(item).model_dump(mode="json")
            for item in items
        ]
        for item, payload in zip(items, serialized_items):
            # resolved_names is keyed by uuid.UUID — payload["id"] from
            # model_dump(mode="json") is a string, so look up by the ORM id.
            entry = resolved_names.get(item.id)
            if entry:
                value, serving = entry
                payload["name"] = value
                # V-08: resolved_language reflects the NAME's serving language
                # only — objects whose name a translation didn't serve stay None.
                payload["resolved_language"] = serving
        await set_cached_with_tags(
            cache_key,
            {"items": serialized_items, "total": total},
            tags=[build_tag_key("object")],
            ttl=CACHE_TTL_LIST,
        )
        return serialized_items, total

    await set_cached_with_tags(
        cache_key,
        {
            # 224-04 task 2: response-model serialization for the no-lang
            # store too (AGENTS.md hard rule) — the payload shape stays
            # byte-identical to the previous encoder output through
            # response_model validation (D-19, proven by test_objects.py).
            "items": [
                ObjectResponse.model_validate(item).model_dump(mode="json")
                for item in items
            ],
            "total": total,
        },
        tags=[build_tag_key("object")],
        ttl=CACHE_TTL_LIST,
    )
    return items, total


async def object_exists(db: AsyncSession, object_id: uuid.UUID) -> bool:
    """Cheap parent-Object existence probe (no cache, no permission work).

    Mirrors the REST route's ``_object_exists`` guard so a caller that writes a
    child row (ObjectComment / ObjectFile) can return a typed not-found before
    the FK constraint rejects the insert — the SDK adapters use it to keep an
    unknown/foreign ``object_id`` from surfacing as a raw ``IntegrityError``.
    """
    result = await db.execute(select(Object.id).where(Object.id == object_id))
    return result.scalar_one_or_none() is not None


async def get_object(
    db: AsyncSession, object_id: uuid.UUID,
    lang: str | None = None,  # V-06 name resolution (224-04)
) -> Object | dict | None:
    # 224-04: lang-scoped entity key (221 MULTI-06 pattern) — a ?lang=fr
    # request can never serve a cached ?lang=en payload; absent lang keeps
    # today's key exactly (no cache invalidation breakage).
    cache_key = build_cache_key(
        "object", f"{object_id}:{lang}" if lang else str(object_id)
    )
    cached = await get_cached(cache_key)
    if (
        cached is not None
        and isinstance(cached, dict)
        and isinstance(cached.get("classification_labels"), list)
    ):
        return cached
    result = await db.execute(select(Object).where(Object.id == object_id))
    obj = result.scalar_one_or_none()
    if obj:
        deny_ids = await _batch_check_has_deny_record_permission(
            db, [obj.id], "object",
        )
        obj.has_deny_record_permission = str(obj.id) in deny_ids
        any_perm_ids = await _batch_check_has_record_permission(
            db, [obj.id], "object",
        )
        obj.has_record_permission = str(obj.id) in any_perm_ids

        # T-317.1-24: the labels are attached before the payload is built, so
        # a cached entry and a freshly assembled one agree. A cached dict
        # without the key is treated as a miss above and rebuilt.
        await attach_classifications(db, "object", [obj])

        # T-223-18: never cache an unresolved payload (no-op today — values
        # aren't eager-loaded by this path).
        obj = _resolve_select_display_loaded(obj)

        if lang:
            # 224-04: V-06 name resolution before the cache set; serialized
            # via the response model (AGENTS.md hard rule — resolved_language
            # is computed and must survive the cache round-trip).
            resolved_names = await _resolve_names_in_place_lang(db, [obj], lang)
            payload = ObjectResponse.model_validate(obj).model_dump(mode="json")
            entry = resolved_names.get(obj.id)
            if entry:
                value, serving = entry
                payload["name"] = value
                payload["resolved_language"] = serving
        else:
            # 224-04 task 2: response-model serialization for the no-lang
            # store (AGENTS.md hard rule) — byte-identical wire output through
            # response_model validation (D-19, proven by test_objects.py).
            payload = ObjectResponse.model_validate(obj).model_dump(mode="json")
        await set_cached_with_tags(
            cache_key,
            payload,
            tags=[build_tag_key("object")],
            ttl=CACHE_TTL_ENTITY,
        )
    if lang:
        return payload if obj else None
    return obj


async def _guard_locked_name_mirror_write(
    db: AsyncSession, obj: Object, *, origin=None
) -> None:
    """290 D-02/D-05: deny a rename that would write an externally locked Name mirror.

    Resolves the same own-first/inherited Name OTA the rename mirror below uses
    and guards ONLY actual mirror writes — an existing Name row being updated
    or a label-backed row about to be created. A foreign-label type whose
    rename creates no Name row stays allowed (D-03).
    """
    name_ota = await resolve_name_attribute(db, obj.object_type_id, system_only=False)
    if name_ota is None:
        return

    existing = await db.execute(
        select(ObjectValue.id).where(
            ObjectValue.object_id == obj.id,
            ObjectValue.object_type_attribute_id == name_ota.id,
        )
    )
    if existing.scalar_one_or_none() is None:
        object_type = await db.get(ObjectType, obj.object_type_id)
        label_id = object_type.label_attribute_id if object_type else None
        if label_id is not None and label_id != name_ota.id:
            return

    await assert_value_writable(
        db,
        object_type_id=obj.object_type_id,
        attribute_id=name_ota.id,
        origin=origin,
        object_id=obj.id,
    )


async def _purge_object_public_cache(obj: Object) -> None:
    """Purge one object's public cache entry (D-19) without an import cycle.

    The helper is imported lazily here: ``app.public_api`` modules are not part
    of this service's import chain, and the public write module pair
    (``object_write`` ↔ ``object_write_service``) has a documented import order.
    Importing through ``object_write`` — which re-exports the purge helper —
    keeps that order intact; importing the service module directly fails when
    it has not been loaded yet.
    Only the object's own entry is purged: a label change does not change the
    hierarchy-shaped public payloads.
    """
    from app.public_api.object_write import purge_public_object_cache

    await purge_public_object_cache(
        str(obj.id), str(obj.object_type_id), str(obj.schema_id), include_hierarchy=False
    )


async def update_object(
    db: AsyncSession,
    object_id: uuid.UUID,
    object_update: ObjectUpdate,
    actor_id: uuid.UUID,
    *,
    origin: hook_origin.MutationOrigin | None = None,
    commit: bool = True,
    emit_index_outbox: bool = True,
    invalidate_cache: bool = True,
) -> Object | None:
    """Update one Object; the composite-writer flags mirror ``create_object``."""
    result = await db.execute(select(Object).where(Object.id == object_id))
    obj = result.scalar_one_or_none()
    if not obj:
        return None

    old_values = model_to_dict(obj)
    # D-19: a PATCH that carries only classification values is not a record
    # edit. It writes the assignment rows, one audit row and the caches — no
    # index outbox row, no ``on_object_updated`` hook, no ``_touch_system_attribute``
    # and therefore no ``updated_at`` bump. "Label-only" means at least one
    # classification field is in ``model_fields_set`` and every other set field
    # is null (an explicit ``name: null`` already means "unchanged" today).
    classification_fields_set = CLASSIFICATION_INPUT_FIELDS & set(
        object_update.model_fields_set
    )
    label_only = bool(classification_fields_set) and all(
        getattr(object_update, field) is None
        for field in set(object_update.model_fields_set) - CLASSIFICATION_INPUT_FIELDS
    )
    if label_only:
        # A ValueError (unknown / wrong-family label id) propagates before any
        # assignment row is written; the route answers 400.
        label_refs_before, label_refs_after = await set_labels(
            db,
            "object",
            object_id,
            **classification_kwargs(object_update, partial=True),
        )
        if classification_audit_keys(label_refs_before) != classification_audit_keys(
            label_refs_after
        ):
            await audit_log(
                db,
                "objects",
                object_id,
                "UPDATE",
                actor_id,
                old_values={
                    "classification_labels": classification_audit_keys(label_refs_before)
                },
                new_values={
                    "classification_labels": classification_audit_keys(label_refs_after)
                },
                object_id=object_id,
            )
        if commit:
            await db.commit()
            await db.refresh(obj)
        else:
            await db.flush()
        if commit and invalidate_cache:
            await invalidate_entity_cache("object")
            await _purge_object_public_cache(obj)
        await attach_classifications(db, "object", [obj])
        return obj

    old_schema_id = obj.schema_id
    old_object_type_id = obj.object_type_id
    # Resolve both candidate rows BEFORE mutating obj so the FINAL
    # Schema/ObjectType pairing can be validated before any attribute is
    # actually changed on the ORM instance (248-10 Gap 3 / CR-02).
    schema = None
    object_type = None
    if object_update.schema_id is not None:
        result = await db.execute(
            select(Schema).where(Schema.id == object_update.schema_id)
        )
        schema = result.scalar_one_or_none()
        if not schema:
            return None
    if object_update.object_type_id is not None:
        result = await db.execute(
            select(ObjectType).where(
                ObjectType.id == object_update.object_type_id
            )
        )
        object_type = result.scalar_one_or_none()
        if not object_type:
            return None
    if object_update.schema_id is not None or object_update.object_type_id is not None:
        final_schema_id = (
            object_update.schema_id if object_update.schema_id is not None
            else obj.schema_id
        )
        final_object_type = object_type
        if final_object_type is None:
            result = await db.execute(
                select(ObjectType).where(ObjectType.id == obj.object_type_id)
            )
            final_object_type = result.scalar_one_or_none()
        if final_object_type is None or final_object_type.schema_id != final_schema_id:
            return None
        if final_object_type.is_abstract and final_object_type.id != obj.object_type_id:
            # INH-02/REVIEW CORRECTION: gate on an ACTUAL type change, not
            # merely on object_type_id being present in the payload — a
            # client resending a D-06 preserved object's current (already
            # abstract) object_type_id unchanged, alongside an unrelated
            # edit, must NOT be rejected (D-06 preserved rows stay editable).
            raise ValueError("Cannot move an Object into an abstract ObjectType")
    if object_update.schema_id is not None and obj.schema_id != object_update.schema_id:
        obj.schema_id = object_update.schema_id
    if object_update.object_type_id is not None and obj.object_type_id != object_update.object_type_id:
        obj.object_type_id = object_update.object_type_id
    schema_moved = old_schema_id != obj.schema_id
    name_changed = object_update.name is not None and obj.name != object_update.name
    if name_changed:
        # 290 D-02/D-05: guard the Name mirror write (existing row update or
        # label-backed creation) before Object.name is touched and before the
        # audit/hook/outbox block below.
        await _guard_locked_name_mirror_write(db, obj, origin=origin)
        obj.name = object_update.name

    # D-07: a classification field in the request replaces exactly those
    # families; an omitted field leaves its family untouched (A4). The labels
    # ride the same transaction as any other record edit — a mixed PATCH keeps
    # the normal update path (outbox, hook, timestamp) and applies both.
    if classification_fields_set:
        label_refs_before, label_refs_after = await set_labels(
            db,
            "object",
            object_id,
            **classification_kwargs(object_update, partial=True),
        )
        old_values["classification_labels"] = classification_audit_keys(
            label_refs_before
        )
    new_values = model_to_dict(obj)
    if classification_fields_set:
        new_values["classification_labels"] = classification_audit_keys(label_refs_after)
    await audit_log(db, "objects", object_id, "UPDATE", actor_id,
                    old_values=old_values, new_values=new_values, object_id=object_id)
    # PLUGCORE-07: fire the object-updated hook in the same transaction as the
    # mutation (D-06), immediately after audit_log. CORESEAM-11/D-14: the payload
    # carries the trusted actor and origin plus the always-present
    # attribute-change keys (empty for a plain Object update — value writes fill
    # them in plans 02/03).
    effective_origin = _resolve_origin(origin, "object.update", actor_id)
    await _fire_object_hook(
        "on_object_updated",
        db,
        object_id=object_id,
        schema_id=obj.schema_id,
        object_type_id=obj.object_type_id,
        old_values=old_values,
        new_values=new_values,
        actor_id=actor_id,
        origin=effective_origin.as_payload(),
        changed_attribute_ids=[],
        attribute_changes=[],
    )
    outbox_entry = None
    remove_outbox_entry = None
    if emit_index_outbox:
        outbox_entry = _new_outbox_entry(
            obj.id, obj.object_type_id, "index",
            actor_id=actor_id, origin=effective_origin,
        )
        db.add(outbox_entry)
        if schema_moved:
            remove_outbox_entry = _new_outbox_entry(
                obj.id, old_object_type_id, "remove", schema_id=old_schema_id,
                actor_id=actor_id, origin=effective_origin,
            )
            db.add(remove_outbox_entry)

    if name_changed:
        # Mirror the reverse direction (object_value_service._sync_object_name):
        # keep the "Name" ObjectValue in step with Object.name so a rename that
        # only touches Object.name here does not leave the queryable name row
        # stale.
        attr_result = await db.execute(
            select(ObjectTypeAttribute).where(
                ObjectTypeAttribute.object_type_id == obj.object_type_id,
                ObjectTypeAttribute.name == "Name",
            )
            # Native ObjectTypes own a system Name binding. Prefer it over an
            # accidental manual duplicate; a type without a system binding keeps
            # its manual multilingual Name attribute selected.
            .order_by(ObjectTypeAttribute.is_system.desc(), ObjectTypeAttribute.id)
            .limit(1)
        )
        name_attr = attr_result.scalar_one_or_none()
        if name_attr is None:
            # 253 D-05a: a Weg-A child owns no Name OTA — its row lives under
            # the defining parent's attribute. Without this fallback the mirror
            # silently no-ops and the inherited row goes stale on every rename.
            inherited_id = await resolve_ancestor_ota_by_name(db, "Name", obj.object_type_id)
            if inherited_id:
                name_attr = await db.get(ObjectTypeAttribute, inherited_id)
        if name_attr:
            value_result = await db.execute(
                select(ObjectValue).where(
                    ObjectValue.object_id == obj.id,
                    ObjectValue.object_type_attribute_id == name_attr.id,
                )
            )
            name_value = value_result.scalar_one_or_none()
            if name_value:
                if name_value.value_simple != obj.name:
                    await delete_translations_for(db, "object_value", [name_value.id])
                name_value.value_simple = obj.name
            else:
                # 253 D-02: only CREATE a Name row when Name is the label. A
                # foreign-label type (e.g. label "IP4") has no required Name
                # field, so a rename must not conjure one up — but an already
                # existing row above keeps tracking Object.name either way,
                # otherwise import/legacy rows would freeze at their old
                # value (SC5: this fix introduces no divergence).
                if object_type is None:
                    object_type = await db.get(ObjectType, obj.object_type_id)
                label_attribute_id = object_type.label_attribute_id if object_type else None
                if label_attribute_id is None or label_attribute_id == name_attr.id:
                    db.add(ObjectValue(
                        object_id=obj.id,
                        object_type_attribute_id=name_attr.id,
                        value_simple=obj.name,
                    ))

    if commit:
        await db.commit()
        await db.refresh(obj)
    else:
        await db.flush()
    await _touch_system_attribute(
        db, obj.id, obj.object_type_id, "Updated", commit=commit
    )
    if not commit:
        return obj
    if outbox_entry is not None:
        await _enqueue_index_job(db, obj.id, "index_object_job", outbox_id=outbox_entry.id)
    if remove_outbox_entry is not None:
        await _enqueue_index_job(
            db,
            obj.id,
            "remove_object_job",
            outbox_id=remove_outbox_entry.id,
            object_type_id=str(old_object_type_id),
            schema_id=str(old_schema_id),
        )
    if invalidate_cache:
        await invalidate_entity_cache("object")
        await invalidate_entity_cache("object_value")
        await invalidate_entity_cache("search")
        if classification_fields_set:
            await _purge_object_public_cache(obj)
    if classification_fields_set:
        await attach_classifications(db, "object", [obj])
    return obj


async def delete_object(
    db: AsyncSession,
    object_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    origin: hook_origin.MutationOrigin | None = None,
) -> bool:
    # selectinload(Object.files) is REQUIRED: the post-delete blob cleanup below
    # reads obj.files as plain application code, and touching an unloaded
    # relationship outside SQLAlchemy's flush machinery raises MissingGreenletError
    # under this codebase's async engine (same failure mode import_execution_service.py
    # documents by name). comments needs no eager load — its rows are removed by the
    # DB-level ON DELETE CASCADE + ORM cascade alone (SC-04 comment half).
    result = await db.execute(
        select(Object).options(selectinload(Object.files)).where(Object.id == object_id)
    )
    obj = result.scalar_one_or_none()
    if not obj:
        return False
    # 290 D-15: an externally managed type blocks deleting its Objects before
    # any row/blob/audit/outbox change — fresh persisted marker under FOR SHARE,
    # never the cache.
    await assert_types_deletable(db, [obj.object_type_id])
    old_values = model_to_dict(obj)
    await audit_log(db, "objects", object_id, "DELETE", actor_id,
                    old_values=old_values, new_values=None, object_id=object_id)
    obj_type_id = obj.object_type_id  # capture before deletion
    obj_schema_id = obj.schema_id  # capture before deletion — index is now keyed by schema, not object type (Phase 248)
    # PLUGCORE-07: fire the object-deleted hook BEFORE db.delete(obj) — mirrors
    # audit_log's documented D-03 ordering rule, so a plugin observing the delete
    # can still read the object's current state. CORESEAM-11/D-14: the payload
    # carries the trusted actor and origin (delete reports the old snapshot only —
    # no attribute-change keys, which belong to created/updated).
    effective_origin = _resolve_origin(origin, "object.delete", actor_id)
    await _fire_object_hook(
        "on_object_deleted",
        db,
        object_id=object_id,
        schema_id=obj_schema_id,
        object_type_id=obj_type_id,
        old_values=old_values,
        actor_id=actor_id,
        origin=effective_origin.as_payload(),
    )
    storage_keys = [f.storage_key for f in obj.files]  # capture before deletion — rows vanish with the CASCADE
    outbox_entry = _new_outbox_entry(
        object_id, obj_type_id, "remove", schema_id=obj_schema_id,
        actor_id=actor_id, origin=effective_origin,
    )
    db.add(outbox_entry)
    await db.delete(obj)
    await db.commit()
    # D-15: DB-first delete — the Object/ObjectFile/ObjectComment rows are gone
    # (single transaction, committed) before any blob-delete attempt. Best-effort
    # (D-16): a failed blob delete never raises out of delete_object() or rolls
    # back the commit — it only leaves a logged orphan blob on disk.
    for key in storage_keys:
        try:
            await get_storage_client().delete(storage_settings.storage_bucket, key)
        except Exception:
            logger.error("orphan blob after failed delete: %s/%s", storage_settings.storage_bucket, key)
    await _enqueue_index_job(
        db,
        object_id,
        "remove_object_job",
        outbox_id=outbox_entry.id,
        object_type_id=str(obj_type_id),
        schema_id=str(obj_schema_id),
    )
    await invalidate_entity_cache("object")
    await invalidate_entity_cache("search")
    return True
