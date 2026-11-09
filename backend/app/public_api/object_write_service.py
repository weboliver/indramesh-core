"""Public object write service (Phase 203): fail-fast validation, value coercion, public cache purge.

This module is the service layer for the public write endpoints
(POST /object/create, PUT /object/{id}, DELETE /object/{id}) — endpoint-independent
logic only. The route handlers in object_write.py call into this module and then
delegate the actual DB writes to the shared service layer (object_service,
object_value_service, object_ref_service) per WRIT-04.

Exports:

- ``purge_public_object_cache`` (D-18..D-21) — SCAN-based, fail-open Redis purge
  for ``cache:public:*`` keys. The public read cache uses ``set_cached`` (no tag),
  so the internal ``invalidate_entity_cache`` tag system cannot reach it — Redis
  SCAN is the only correct approach.

- ``validate_object_payload`` (D-01..D-12) — fail-fast pre-validation pass that
  turns a Jira ``ObjectIn`` payload into a ``WritePlan`` (validated coerced
  write ops). Every error is collected and returned as a 400 with the full
  error list BEFORE any shared-service write — partial writes cannot be
  rolled back (each shared-service call commits independently). For a
  non-superuser the reference-target select is record-filtered, so a DENY'd
  target is indistinguishable from a nonexistent one; superusers pay nothing
  (quick 261012-ca3).

- ``WritePlan`` / ``ResolvedEntry`` dataclasses — the frozen contract for
  downstream plans (203-03..05) to call shared services with.

- 290 D-02/D-06 lock preflight: ``validate_object_payload(preflight_locks=
  True)`` (create) and ``replace_object_values`` (PUT) collect real lock
  conflicts into the public 409 ``errors`` list BEFORE the first write, so a
  mixed locked/unlocked batch is all-or-nothing. A marker flipped between the
  preflight and a later per-value commit is still caught by the shared
  services' own pre-write guards (plan 05, defense in depth) and mapped to
  the same 409. The residual race is accepted: no marker writer exists before
  Phase 293, and the 409 keeps the DB consistent either way.
"""

import json
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.redis import get_redis
from app.models import (
    AttributeType, Object, ObjectRef, ObjectType, ObjectTypeAttribute, ObjectValue, Schema,
)
from app.models.index_outbox import IndexOutboxStatus
from app.public_api.object_read_service import (
    _ATTR_DEFS_CTE_SQL,
    load_visible_reference_targets,
)
from app.public_api.object_write import AttributeEntry, ObjectAttributeValue, ObjectIn
from app.schemas.crud import ObjectRefCreate, ObjectUpdate, ObjectValueCreate
from app.services.cache_service import invalidate_entity_cache
from app.services.object_ref_service import create_object_ref, delete_object_ref
from app.services.object_service import (
    _enqueue_index_job,
    _new_outbox_entry,
    _resolve_origin,
    update_object,
)
from app.services.object_type_service import resolve_ancestor_ota_by_name
from app.services.object_value_service import create_object_value, delete_object_value
from app.services.record_permission_service import (
    build_record_permission_filter,
    denied_record_ids,
)
from app.services.value_lock_service import (
    ValueLockConflict,
    assert_value_writable,
    resolve_name_attribute,
)

# CORESEAM-11/D-14: the Core-owned origin name for a public composite PUT. The
# default is derived only from the already-resolved public consumer actor (the
# synthetic service user), so a client payload can never choose the origin
# (T-291.2-13), and it is distinct from the internal value/ref service defaults.
PUBLIC_OBJECT_UPDATE_ORIGIN = "public-api.object-update"


async def purge_public_object_cache(
    object_id: str,
    object_type_id: str,
    schema_id: str,
    include_hierarchy: bool = True,
) -> None:
    """Best-effort, fail-open public cache purge (D-18..D-21).

    The ``cache:public:*`` keys are written via ``set_cached`` WITHOUT tags
    (``get_public_object`` in object_read_service.py), so the tag-based
    ``invalidate_entity_cache`` in cache_service.py cannot reach them — Redis
    SCAN is the only correct approach.

    Key-pattern contract (MUST match the read side of Phase 201/202 exactly):
      - ``cache:public:object:{id}:*`` — build_public_object_cache_key shape
        (object_read_service.py:378)
      - ``cache:public:objecttype:{id}:*`` — object_types.py public list shape
      - ``cache:public:schema:{id}:*`` and ``cache:public:schemas:list:*`` —
        schemas.py public shape

    On any Redis error the purge silently fails open (D-21) — the 60s public
    object-cache TTL bounds worst-case staleness.
    """
    try:
        redis = await get_redis()
        patterns = [f"cache:public:object:{object_id}:*"]
        if include_hierarchy:
            patterns += [
                f"cache:public:objecttype:{object_type_id}:*",
                f"cache:public:schema:{schema_id}:*",
                f"cache:public:schemas:list:*",
            ]
        for pattern in patterns:
            async for key in redis.scan_iter(pattern):
                await redis.delete(key)
    except Exception:
        # D-21: fail-open — 60s TTL bounds worst-case staleness
        pass


async def invalidate_composite_write_caches() -> None:
    """Bust every list namespace a composite object write can change (D-14).

    Both composite routes — ``POST /public/api/v1/object/create`` and
    ``PUT /public/api/v1/object/{id}`` — call only this helper after their one
    commit, so neither route can forget a namespace again and the "which half
    of the payload changed" question is gone. All four are needed because one
    composite write can change values, references and searchable fields in one
    transaction, and ``object`` covers the object row itself. Fail-open by
    construction: the tag invalidation swallows Redis errors, so a cache outage
    never turns a committed write into an error.

    This is the tag-based list-cache purge only. ``purge_public_object_cache``
    stays a separate, SCAN-based purge of the ``cache:public:*`` keys, which are
    written without tags and therefore unreachable from here.
    """
    for entity_type in ("object", "object_value", "object_ref", "search"):
        await invalidate_entity_cache(entity_type)


@dataclass
class ResolvedEntry:
    """One validated payload attribute entry: definition + coerced write ops.

    The validation pass populates either ``value_ops`` (scalar/status/datetime
    values → ObjectValueCreate instances) or ``ref_ops`` (reference values →
    ObjectRefCreate instances). For reference values the ``from_object_id``
    field is a placeholder (``uuid.UUID(int=0)``) — the caller (Plan 03's
    create adapter / Plan 04's PUT replace adapter) MUST overwrite it with
    the actual FROM object id once known. ``source_id`` is already filled
    in (derived from ``Schema.source_id``) when refs survive validation.
    """

    attr_def: dict  # contract dict (id, name, data_type, maximum_cardinality, structure, referenced_object_type_id, reference_type_id)
    value_ops: List[ObjectValueCreate] = field(default_factory=list)
    ref_ops: List[ObjectRefCreate] = field(default_factory=list)


@dataclass
class WritePlan:
    """Validated payload ready for shared-service delegation.

    ``object_type`` is the resolved ObjectType ORM row. ``source_id`` is
    derived from ``Schema.source_id`` (ObjectRefCreate requires it; the Jira
    wire payload has no source_id). ``label_value`` is the first label-
    attribute value (str) — drives ``Object.name`` via the existing
    ``_sync_object_name`` path. ``label_attr_id`` is the resolved label
    attribute id (FK from ObjectType, with a "Name"-attribute fallback).
    """

    object_type: ObjectType
    schema_id: uuid.UUID
    source_id: Optional[uuid.UUID]  # derived from Schema.source_id (ObjectRefCreate requirement)
    label_value: Optional[str]  # D-04: first label-attribute value (str) — drives Object.name
    label_attr_id: Optional[uuid.UUID]
    entries: List[ResolvedEntry]


def _coerce_status_value(
    attr_def: dict,
    ota_id: uuid.UUID,
    raw: Any,
) -> Optional[ObjectValueCreate]:
    """Coerce a status value to ObjectValueCreate(value_struct={"id": ...}).

    Mirrors object_value_service._validate_status_value: the value must be
    a UUID-string present in the attribute's ``structure.status_options``.
    Returns None on coercion failure.

    ``object_id`` is a placeholder (uuid.UUID(int=0)) — the caller fills it
    in after the FROM object is created in Plan 03's create adapter.
    ``object_type_attribute_id`` is filled from the resolved OTA id (known
    at validation time).
    """
    if not isinstance(raw, str):
        return None
    try:
        uuid.UUID(raw)
    except (ValueError, TypeError):
        return None
    structure = attr_def.get("structure") or {}
    status_options = structure.get("status_options") or []
    valid_ids = {opt.get("id") for opt in status_options}
    if raw not in valid_ids:
        return None
    return ObjectValueCreate(
        object_id=uuid.UUID(int=0),  # placeholder; caller fills
        object_type_attribute_id=ota_id,
        value_struct={"id": str(raw)},
    )


def _resolve_select_option(attr_def: dict, raw: str) -> str:
    """Resolve a SELECT display string to its canonical option id (D-05/D-18).

    Same resolution order as object_value_service._validate_select_value:
      1. raw is already a known option id (exact string match on the id set) ->
         unchanged (idempotent pass-through);
      2. raw equals an option's "value" -> that option's "id";
      3. else -> raw UNCHANGED (D-15): the public write contract is permissive
         (Phase 203 D-09 strict — any string is accepted), so unknown values
         are stored raw, never fabricated and never rejected.

    Pre-migration (id-less) structures also pass raw through unchanged.
    """
    options = (attr_def.get("structure") or {}).get("options") or []
    option_ids = {str(opt.get("id")) for opt in options if opt.get("id")}
    if raw in option_ids:
        return raw
    for opt in options:
        if opt.get("value") == raw and opt.get("id"):
            return str(opt["id"])
    return raw


def _coerce_simple_value(
    attr_name: str,
    ota_id: uuid.UUID,
    raw: Any,
    dt_value: str,
    attr_def: Optional[dict] = None,
) -> Tuple[Optional[ObjectValueCreate], Optional[str]]:
    """Coerce a single payload value (non-reference, non-status) to ObjectValueCreate.

    Returns ``(op, None)`` on success, ``(None, error_string)`` on failure.
    Reference and status are handled separately because they need extra
    context (target-object existence, status_options).

    ``object_id`` is a placeholder; ``object_type_attribute_id`` is the
    resolved OTA id. ``attr_def`` (the resolved attribute definition dict,
    incl. structure) is required for the SELECT resolution branch.
    """
    common = {"object_id": uuid.UUID(int=0), "object_type_attribute_id": ota_id}

    if dt_value in ("text", "select", "url", "email", "json", "version", "group", "jira", "confluence"):
        if not isinstance(raw, str):
            return None, f"Attribute '{attr_name}': expected string, got {type(raw).__name__}"
        value_simple = raw
        if dt_value == "select":
            # D-05: known display strings resolve to the option id; unknown
            # values stay raw (D-15, permissive public contract).
            value_simple = _resolve_select_option(attr_def or {}, raw)
        return ObjectValueCreate(**common, value_simple=value_simple), None

    if dt_value == "integer":
        if isinstance(raw, bool) or not isinstance(raw, int):
            return None, f"Attribute '{attr_name}': expected integer"
        return ObjectValueCreate(**common, value_integer=raw), None

    if dt_value == "double":
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None, f"Attribute '{attr_name}': expected number"
        return ObjectValueCreate(**common, value_float=float(raw)), None

    if dt_value == "boolean":
        if not isinstance(raw, bool):
            return None, f"Attribute '{attr_name}': expected boolean"
        return ObjectValueCreate(**common, value_boolean=raw), None

    if dt_value == "datetime":
        if not isinstance(raw, str):
            return None, f"Attribute '{attr_name}': expected ISO-8601 datetime string"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None, f"Attribute '{attr_name}': expected ISO-8601 datetime string"
        # The ObjectValue.value_datetime column is TIMESTAMP WITHOUT TIME ZONE
        # (Postgres naive timestamp). asyncpg raises
        # "can't subtract offset-naive and offset-aware datetimes" if a
        # tz-aware datetime is bound to a naive column. Plan 06's
        # TestRoundTrip::test_roundtrip_datetime surfaced this on the PUT
        # path (Plan 03's create path had the same bug — any ISO-8601
        # string with a timezone offset would 500 on write). Normalize
        # the parsed datetime to naive (assumes the consumer's offset is
        # canonical — the wire format is local-time-with-offset, the
        # stored value is the local-time-as-naive-timestamp; the
        # display side renders the naive value back as-is via
        # ``dt.isoformat()`` in object_shape._build_value_shape).
        if parsed.tzinfo is not None:
            parsed = parsed.replace(tzinfo=None)
        return ObjectValueCreate(**common, value_datetime=parsed), None

    return None, f"Attribute '{attr_name}': unsupported data type {dt_value}"


def _parse_reference_target(
    attr_name: str,
    raw: Any,
) -> Tuple[Optional[uuid.UUID], Optional[str]]:
    """Parse a reference value's raw payload value to a target UUID.

    Returns ``(uuid, None)`` on success, ``(None, error_string)`` on failure.
    Existence + type-match checks happen later in the batch validation pass.
    """
    if not isinstance(raw, str):
        return None, f"Attribute '{attr_name}': expected reference object UUID"
    try:
        return uuid.UUID(raw), None
    except (ValueError, TypeError):
        return None, f"Attribute '{attr_name}': expected reference object UUID"


async def _lock_conflict_message(
    db: AsyncSession,
    object_type_id: uuid.UUID,
    attribute_id: uuid.UUID,
    *,
    object_id: Optional[uuid.UUID] = None,
) -> Optional[str]:
    """Return the canonical 409 message when the effective value lock applies.

    Uses the plan-05 persisted resolver (own ObjectType OR the resolved OTA,
    fresh ``FOR SHARE`` read — never the metadata cache). A concrete
    ``object_id`` lets a per-object release (294 D-11) clear the conflict for
    every writer. Non-raising by design: the public surface collects every
    conflict into its ``errors`` list instead of aborting on the first one
    (D-04/D-06).
    """
    try:
        await assert_value_writable(
            db,
            object_type_id=object_type_id,
            attribute_id=attribute_id,
            object_id=object_id,
        )
    except ValueLockConflict as exc:
        return str(exc)
    return None


async def _preflight_implicit_name_mirror(
    db: AsyncSession,
    object_type: ObjectType,
    checked_attribute_ids: set[uuid.UUID],
) -> Optional[str]:
    """Guard the Name ObjectValue ``create_object`` mirrors from the label.

    ``create_object`` mirrors the user-supplied label into a Name row whenever
    the type's label binding is absent or named "Name" — resolved own-first
    over an inherited system Name. That write is implicit (the client sends
    only the label entry), so when the label OTA differs from the system Name
    binding the submitted-entry pass above cannot see it. Returns a conflict
    message, or None when no implicit mirror write happens.
    """
    label_ota = (
        await db.get(ObjectTypeAttribute, object_type.label_attribute_id)
        if object_type.label_attribute_id else None
    )
    if label_ota is not None and label_ota.name != "Name":
        return None
    name_ota = await resolve_name_attribute(db, object_type.id, system_only=True)
    if name_ota is None or name_ota.id in checked_attribute_ids:
        return None
    return await _lock_conflict_message(db, object_type.id, name_ota.id)


async def _assert_attribute_type_actions_allowed(
    db: AsyncSession,
    attribute_type_ids,
    *,
    user_id: str,
    is_superuser: bool,
    action: str,
) -> None:
    """403 when an attribute type's record DENY blocks ``action``.

    The internal value routes run ``assert_record_action_allowed`` before every
    value write/delete; the composite public write path must enforce the same
    stored ``write``/``create``/``delete`` DENY or it becomes a bypass for the
    identical rows. The public 4xx envelope is ``{"errors": [...]}``, matching
    the other write errors.

    REV-03 / D-12: the whole attribute-type set is checked by ONE batched
    ``record_permissions`` statement on the caller's session
    (``denied_record_ids``), so a cold payload touching many attribute types
    costs one role resolution and one statement instead of one resolver session
    per attribute type.
    """
    if is_superuser:
        return
    unique_ids = list(
        dict.fromkeys(
            uuid.UUID(str(attribute_type_id))
            for attribute_type_id in attribute_type_ids
            if attribute_type_id is not None
        )
    )
    if not unique_ids:
        return
    denied = await denied_record_ids(
        db, user_id, "attribute_type", unique_ids, action
    )
    if denied:
        raise HTTPException(
            status_code=403,
            detail={"errors": ["Permission denied for this attribute type"]},
        )


async def assert_object_delete_attribute_types_allowed(
    db: AsyncSession,
    object_id: uuid.UUID,
    *,
    user_id: str,
    is_superuser: bool,
) -> None:
    """403 when an attribute type carried by the object denies ``delete``.

    The public composite DELETE removes every value and reference of the object
    in one call; the per-value DELETE route checks the attribute-type
    ``delete`` gate for each row, so the composite path reproduces that gate
    before the object cascade runs. Scalar values and name-keyed references are
    both covered; a nameless legacy reference belongs to no attribute.
    """
    if is_superuser:
        return

    attribute_type_ids = (
        await db.execute(
            select(ObjectTypeAttribute.attribute_type_id)
            .join(
                ObjectValue,
                ObjectValue.object_type_attribute_id == ObjectTypeAttribute.id,
            )
            .where(ObjectValue.object_id == object_id)
            .distinct()
        )
    ).scalars().all()

    ref_names = set(
        (
            await db.execute(
                select(ObjectRef.from_attribute_name).where(
                    ObjectRef.from_object_id == object_id,
                    ObjectRef.from_attribute_name.is_not(None),
                )
            )
        ).scalars().all()
    )
    if ref_names:
        object_type_id = (
            await db.execute(
                select(Object.object_type_id).where(Object.id == object_id)
            )
        ).scalar_one_or_none()
        if object_type_id is not None:
            rows = (
                await db.execute(
                    text(_ATTR_DEFS_CTE_SQL), {"object_type_id": object_type_id}
                )
            ).fetchall()
            for row in rows:
                if row.name in ref_names:
                    attribute_type_ids.append(row.attribute_type_id)

    await _assert_attribute_type_actions_allowed(
        db,
        attribute_type_ids,
        user_id=user_id,
        is_superuser=is_superuser,
        action="delete",
    )


async def validate_object_payload(
    db: AsyncSession,
    payload: ObjectIn,
    object_type_id: uuid.UUID,
    *,
    require_label: bool = True,
    preflight_locks: bool = False,
    user_id: str,
    is_superuser: bool,
) -> WritePlan:
    """Fail-fast pre-validation pass: payload → WritePlan (D-01..D-12).

    Algorithm (in order, deterministic on payload entry order):

    1. Resolve ObjectType (400 if unknown, 400 ``{"errors": [...]}`` if it is
       abstract — CR314-47, before any shared-service call).
    2. Resolve attribute definitions via the inherited CTE
       (``_ATTR_DEFS_CTE_SQL`` — nearest ancestor wins, DISTINCT ON name).
    3. Batch-load ``data_type`` from AttributeType rows.
    4. Per payload entry: validate objectTypeAttributeId, read-only names,
       cardinality, per-value coercion; collect errors and continue.
    5. Label-attribute presence check (D-04) — required when
       ``require_label=True`` (create), optional when ``require_label=False``
       (PUT replace).
    6. Batch reference-target validation (D-12): targets exist + match
       ``referenced_object_type_id``. For a non-superuser the target select
       is record-filtered, so a DENY'd target is indistinguishable from a
       nonexistent one; superusers pay nothing (quick 261012-ca3).
    7. Derive ``source_id`` from the ObjectType's Schema (only required when
       at least one ref op survived).
    8. Raise HTTPException(400, ``{"errors": [...]}``) on any error.
    9. Build ObjectRefCreate ops for surviving reference targets — note
       ``object_type_id`` is the FROM side's type (the native ObjectRef
       contract; see 203-RESEARCH.md Pattern 4).

    With ``preflight_locks=True`` (the public create path) a persisted
    effective-lock preflight runs over every surviving entry plus the implicit
    user-supplied Name mirror ``create_object`` writes from the label. Real
    conflicts raise HTTPException(409, ``{"errors": [...]}``) — but only when
    no unrelated validation error exists: 400 wins, so the documented
    validation semantics stay unchanged (D-04/D-06).
    """
    errors: List[str] = []
    lock_conflicts: List[str] = []
    locked_checked: set[uuid.UUID] = set()

    # 1. Resolve ObjectType
    ot_result = await db.execute(select(ObjectType).where(ObjectType.id == object_type_id))
    object_type = ot_result.scalar_one_or_none()
    if not object_type:
        raise HTTPException(status_code=400, detail="Unknown objectTypeId")

    # CR314-47: an abstract ObjectType never holds Objects — the shared
    # create_object raises the same condition as a ValueError. Rejecting it
    # here, before any shared-service call, answers the public contract with a
    # named 4xx and the usual {"errors": [...]} envelope instead of a 5xx after
    # a partial write. 400 (not 422) so the envelope shape stays identical to
    # every other rejection this pass produces.
    if object_type.is_abstract:
        raise HTTPException(
            status_code=400,
            detail={"errors": ["Object type is abstract and cannot hold objects"]},
        )

    # 2. Resolve attribute definitions (CTE inheritance)
    defs_result = await db.execute(
        text(_ATTR_DEFS_CTE_SQL), {"object_type_id": object_type_id}
    )
    defs_rows = defs_result.fetchall()
    defs = _attrs_to_dicts(defs_rows)
    defs_by_id = {d["id"]: d for d in defs}

    # 3. Batch-load data_type
    if defs:
        attr_type_ids = {
            uuid.UUID(d["attribute_type_id"]) for d in defs if d["attribute_type_id"]
        }
        if attr_type_ids:
            at_result = await db.execute(
                select(AttributeType).where(AttributeType.id.in_(attr_type_ids))
            )
            data_types = {at.id: at.data_type for at in at_result.scalars().all()}
            for d in defs:
                at_id = uuid.UUID(d["attribute_type_id"])
                d["data_type"] = data_types.get(at_id)

    # Determine label_attr_id
    label_attr_id: Optional[uuid.UUID] = object_type.label_attribute_id
    if label_attr_id is None:
        # Fallback: first attribute named "Name" (mirrors _sync_object_name
        # semantics — the label attribute is the one named "Name")
        name_fallback = next(
            (d for d in defs if d["name"] == "Name"),
            None,
        )
        if name_fallback:
            label_attr_id = uuid.UUID(name_fallback["id"])

    # Per-entry collection: ref targets (UUIDs) + the attr_def each came from
    # (so type-match checks can find the right referenced_object_type_id).
    ref_targets: List[Tuple[uuid.UUID, dict]] = []

    # 4. Per-entry validation
    entries: List[ResolvedEntry] = []
    label_seen = False
    label_value: Optional[str] = None

    for entry in payload.attributes:
        attr_def = defs_by_id.get(str(entry.objectTypeAttributeId))
        if attr_def is None:
            errors.append(f"Unknown objectTypeAttributeId: {entry.objectTypeAttributeId}")
            continue

        attr_name = attr_def["name"]

        # D-02 read-only: name-based check (Key/Created/Updated are the Jira
        # editable: false set). NOT is_required-based — user-defined required
        # attributes must pass.
        if attr_name in ("Key", "Created", "Updated"):
            errors.append(f"Attribute '{attr_name}' is read-only")
            continue

        # D-05 cardinality
        if (
            attr_def["maximum_cardinality"] == 1
            and len(entry.objectAttributeValues) > 1
        ):
            errors.append(f"Attribute '{attr_name}': cardinality violation")
            continue

        # Zero-value entries (final rule — unambiguous): create mode
        # (require_label=True) requires ≥1 value per entry; PUT replace mode
        # (require_label=False) treats zero-value as a legal PUT-clear.
        if len(entry.objectAttributeValues) == 0 and require_label:
            errors.append(f"Attribute '{attr_name}': no values provided")
            continue

        value_ops: List[ObjectValueCreate] = []
        ref_ops: List[ObjectRefCreate] = []

        for v in entry.objectAttributeValues:
            raw = v.value
            dt = attr_def.get("data_type")
            ota_id = uuid.UUID(attr_def["id"])

            if dt is None:
                errors.append(
                    f"Attribute '{attr_name}': unsupported data type (no attribute type)"
                )
                continue

            dt_value = dt.value if hasattr(dt, "value") else str(dt)

            if dt_value == "status":
                op = _coerce_status_value(attr_def, ota_id, raw)
                if op is None:
                    errors.append(
                        f"Status value {raw!r} does not match a known Status option for attribute '{attr_name}'"
                    )
                    continue
                value_ops.append(op)
                continue

            if dt_value == "reference":
                target_uuid, err = _parse_reference_target(attr_name, raw)
                if err is not None:
                    errors.append(err)
                    continue
                ref_targets.append((target_uuid, attr_def))
                # Ref op is built after step 7 (source_id) — placeholder here
                ref_ops.append(
                    ObjectRefCreate(
                        from_object_id=uuid.UUID(int=0),  # placeholder; caller fills
                        to_object_id=target_uuid,
                        object_type_id=object_type.id,  # FROM side (Pattern 4)
                        source_id=uuid.UUID(int=0),  # placeholder; filled after step 7
                        from_attribute_name=attr_name,
                    )
                )
                continue

            op, err = _coerce_simple_value(attr_name, ota_id, raw, dt_value, attr_def=attr_def)
            if err is not None:
                errors.append(err)
                continue
            value_ops.append(op)

        # 290 D-02/D-04: a surviving write op into an externally managed value
        # is a real conflict. Collected (not raised) so the caller can separate
        # lock conflicts from unrelated 400 validation errors.
        if preflight_locks and (value_ops or ref_ops):
            ota_uuid = uuid.UUID(attr_def["id"])
            if ota_uuid not in locked_checked:
                locked_checked.add(ota_uuid)
                conflict = await _lock_conflict_message(db, object_type.id, ota_uuid)
                if conflict is not None:
                    lock_conflicts.append(conflict)

        # D-04 label detection: if this entry IS the label, record value
        if (
            label_attr_id is not None
            and str(entry.objectTypeAttributeId) == str(label_attr_id)
            and entry.objectAttributeValues
        ):
            label_seen = True
            first = entry.objectAttributeValues[0].value
            if isinstance(first, str):
                label_value = first

        entries.append(
            ResolvedEntry(
                attr_def=attr_def,
                value_ops=value_ops,
                ref_ops=ref_ops,
            )
        )

    # 5. D-04 label check
    if require_label and label_attr_id is not None and not label_seen:
        errors.append("Label attribute missing from payload")
    if require_label and label_attr_id is None:
        errors.append("Object type has no label attribute")

    # 6. Reference batch validation (D-12)
    surviving_targets: set[uuid.UUID] = set()
    if ref_targets:
        target_ids = {tid for tid, _ in ref_targets}
        # quick 261012-ca3: a target the caller may not read is reported
        # exactly like a nonexistent one, before the type check.
        target_query = select(Object).where(Object.id.in_(target_ids))
        object_filter = await build_record_permission_filter(
            db, user_id, "object", is_superuser=is_superuser
        )
        if object_filter is not None:
            target_query = target_query.where(object_filter)
        rt_result = await db.execute(target_query)
        existing_targets = {o.id: o for o in rt_result.scalars().all()}
        for target_id, _attr_def in ref_targets:
            target = existing_targets.get(target_id)
            if target is None:
                errors.append(f"Reference target {target_id} not found")
                continue
            expected_type_id = (
                uuid.UUID(_attr_def["referenced_object_type_id"])
                if _attr_def.get("referenced_object_type_id")
                else None
            )
            if expected_type_id is not None and target.object_type_id != expected_type_id:
                errors.append(
                    f"Reference target {target_id} does not match the referenced object type"
                )
                continue
            surviving_targets.add(target_id)

    # 7. source_id derivation
    source_id: Optional[uuid.UUID] = None
    if ref_targets:
        schema_result = await db.execute(
            select(Schema).where(Schema.id == object_type.schema_id)
        )
        schema = schema_result.scalar_one_or_none()
        if schema is None:
            errors.append("Object type schema not found")
        else:
            source_id = schema.source_id

    # 7b. 290 D-02: the implicit user-supplied Name mirror create_object
    #     writes from the label — preflight the effective OT OR the actual
    #     own-first/inherited system Name binding it will write.
    if preflight_locks:
        mirror_conflict = await _preflight_implicit_name_mirror(
            db, object_type, locked_checked
        )
        if mirror_conflict is not None:
            lock_conflicts.append(mirror_conflict)

    # 8. Raise on any error — unrelated validation failures keep their 400;
    #    lock conflicts answer 409 with the same {"errors": [...]} contract
    #    (D-04/D-06). 400 wins when both exist.
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})
    # 8b. CR-03: the same attribute-type record DENY the internal value routes
    #     enforce. Runs only over surviving entries (a zero-value entry is a
    #     PUT clear, i.e. an update), before any shared-service write.
    await _assert_attribute_type_actions_allowed(
        db,
        (uuid.UUID(entry.attr_def["attribute_type_id"]) for entry in entries),
        user_id=user_id,
        is_superuser=is_superuser,
        action="create" if require_label else "update",
    )
    if lock_conflicts:
        raise HTTPException(status_code=409, detail={"errors": lock_conflicts})

    # 9. Build final ref ops (with real source_id; drop targets that failed
    # the type-match check)
    final_entries: List[ResolvedEntry] = []
    for entry in entries:
        if not entry.ref_ops:
            final_entries.append(entry)
            continue
        ref_ops_final: List[ObjectRefCreate] = []
        for ref_op in entry.ref_ops:
            if ref_op.to_object_id not in surviving_targets:
                continue
            ref_ops_final.append(
                ObjectRefCreate(
                    from_object_id=ref_op.from_object_id,  # placeholder; caller fills
                    to_object_id=ref_op.to_object_id,
                    object_type_id=object_type.id,  # FROM side (Pattern 4)
                    source_id=source_id,  # resolved
                    from_attribute_name=ref_op.from_attribute_name,
                )
            )
        final_entries.append(
            ResolvedEntry(
                attr_def=entry.attr_def,
                value_ops=entry.value_ops,
                ref_ops=ref_ops_final,
            )
        )

    return WritePlan(
        object_type=object_type,
        schema_id=object_type.schema_id,
        source_id=source_id,
        label_value=label_value,
        label_attr_id=label_attr_id,
        entries=final_entries,
    )


def _attrs_to_dicts(rows: Any) -> List[dict]:
    """Map CTE rows to attribute_def contract dicts (mirrors _attr_def_dict in object_read_service)."""
    out: List[dict] = []
    for row in rows:
        out.append(
            {
                "id": str(row.id),
                "name": row.name,
                "description": row.description,
                "sort": row.sort,
                "attribute_type_id": str(row.attribute_type_id),
                "minimum_cardinality": row.minimum_cardinality,
                "maximum_cardinality": row.maximum_cardinality,
                "is_required": row.is_required,
                "is_hidden": row.is_hidden,
                "is_unique": row.is_unique,
                "structure": row.structure,
                "referenced_object_type_id": (
                    str(row.referenced_object_type_id) if row.referenced_object_type_id else None
                ),
                "reference_type_id": str(row.reference_type_id) if row.reference_type_id else None,
                "data_type": None,
            }
        )
    return out


def _scalar_value_key(value: Any) -> Tuple:
    """Canonical comparison key for a persisted or requested scalar value.

    Works for both ``ObjectValue`` rows and ``ObjectValueCreate`` ops (same
    column attribute names). ``value_struct`` is JSON-normalized so a status
    dict compares by content, not identity.
    """
    return (
        value.value_simple,
        value.value_integer,
        value.value_float,
        value.value_boolean,
        value.value_datetime,
        json.dumps(value.value_struct, sort_keys=True, default=str)
        if value.value_struct is not None else None,
    )


def _is_reference_def(attr_def: dict) -> bool:
    dt = attr_def.get("data_type")
    dt_value = dt.value if hasattr(dt, "value") else (str(dt) if dt is not None else None)
    return dt_value == "reference"


async def _entry_matches_persisted(
    db: AsyncSession,
    obj: Object,
    entry: ResolvedEntry,
    system_name_ota_id: Optional[uuid.UUID],
    *,
    visible_targets: dict[str, str] | None,
) -> bool:
    """D-05 no-op detection: does the passed entry equal the persisted state?

    Compares the normalized typed/status/SELECT scalar multiset or the
    reference target-UUID multiset (order-insensitive, cardinality preserved).
    A passed entry with an explicitly empty set is unchanged only when the
    persisted set is already empty — otherwise it is a real clear. With
    ``visible_targets`` set only the caller-visible persisted references are
    compared (quick 261012-cpp).
    """
    ota_id = uuid.UUID(entry.attr_def["id"])

    # The system Name OTA is exempt from delete-then-recreate (253 Pitfall 7):
    # its row mirrors Object.name. Compare the requested label against the
    # persisted row (falling back to Object.name when no row exists yet).
    if system_name_ota_id is not None and ota_id == system_name_ota_id:
        persisted_name = (
            await db.execute(
                select(ObjectValue.value_simple).where(
                    ObjectValue.object_id == obj.id,
                    ObjectValue.object_type_attribute_id == ota_id,
                )
            )
        ).scalar_one_or_none()
        if persisted_name is None:
            persisted_name = obj.name
        persisted_names = [persisted_name] if persisted_name is not None else []
        return Counter(op.value_simple for op in entry.value_ops) == Counter(
            persisted_names
        )

    if _is_reference_def(entry.attr_def):
        persisted_refs = (
            await db.execute(
                select(ObjectRef.to_object_id).where(
                    ObjectRef.from_object_id == obj.id,
                    ObjectRef.from_attribute_name == entry.attr_def["name"],
                )
            )
        ).scalars().all()
        if visible_targets is not None:
            persisted_refs = [
                to_id for to_id in persisted_refs if str(to_id) in visible_targets
            ]
        return Counter(persisted_refs) == Counter(
            rop.to_object_id for rop in entry.ref_ops
        )

    persisted_values = (
        await db.execute(
            select(ObjectValue).where(
                ObjectValue.object_id == obj.id,
                ObjectValue.object_type_attribute_id == ota_id,
            )
        )
    ).scalars().all()
    return Counter(_scalar_value_key(v) for v in persisted_values) == Counter(
        _scalar_value_key(op) for op in entry.value_ops
    )


async def _rename_will_write_name_mirror(
    db: AsyncSession,
    obj: Object,
    object_type: ObjectType,
    mirror_ota_id: uuid.UUID,
) -> bool:
    """Whether ``update_object``'s rename mirror will touch ``mirror_ota_id``.

    Mirrors ``object_service._guard_locked_name_mirror_write``'s write detection:
    an existing Name row is always kept in sync; a missing row is created only
    when the type's label attribute IS the Name OTA (or the type has no label
    attribute). A foreign-label type with no Name row writes nothing (D-03), so
    its rename must not be blocked by a lock on the resolved Name binding.
    """
    existing = await db.execute(
        select(ObjectValue.id).where(
            ObjectValue.object_id == obj.id,
            ObjectValue.object_type_attribute_id == mirror_ota_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return True
    label_id = object_type.label_attribute_id if object_type else None
    return label_id is None or label_id == mirror_ota_id


async def _preflight_replace_locks(
    db: AsyncSession,
    obj: Object,
    plan: WritePlan,
    system_name_ota_id: Optional[uuid.UUID],
    *,
    visible_targets: dict[str, str] | None,
) -> set[int]:
    """Classify PUT entries before the first destructive write.

    Returns the indices of entries that already equal the persisted state
    (safe to skip: no delete/recreate/audit). For every real change, collects
    the effective persisted lock conflict — over the entry's own resolved OTA
    and, for the entry that drives the name mirror (a literal "Name" entry or
    the type's label attribute), over the own-first/inherited binding
    ``update_object`` will actually mirror into. Raises the single 409
    ``{"errors": [...]}`` before any delete/create when conflicts exist.
    """
    skip_indices: set[int] = set()
    conflicts: List[str] = []
    seen_messages: set[str] = set()
    checked_mirror_ids: set[uuid.UUID] = set()

    for index, entry in enumerate(plan.entries):
        if await _entry_matches_persisted(
            db, obj, entry, system_name_ota_id, visible_targets=visible_targets
        ):
            skip_indices.add(index)
            continue

        ota_id = uuid.UUID(entry.attr_def["id"])
        conflict = await _lock_conflict_message(
            db, obj.object_type_id, ota_id, object_id=obj.id
        )
        if conflict is not None and conflict not in seen_messages:
            seen_messages.add(conflict)
            conflicts.append(conflict)

        # A rename drives Object.name from the label attribute, so the entry
        # that OWNS the label binding — whether it is literally named "Name" or
        # a foreign label — is the one update_object's mirror will write. Guard
        # its resolved Name binding too, but only when the mirror actually
        # writes (existing row, or the label binding creates one).
        drives_name_mirror = (
            entry.attr_def["name"] == "Name"
            or (
                plan.label_attr_id is not None
                and ota_id == plan.label_attr_id
            )
        )
        if drives_name_mirror:
            mirror_ota = await resolve_name_attribute(
                db, obj.object_type_id, system_only=False
            )
            if (
                mirror_ota is not None
                and mirror_ota.id != ota_id
                and mirror_ota.id not in checked_mirror_ids
                and await _rename_will_write_name_mirror(
                    db, obj, plan.object_type, mirror_ota.id
                )
            ):
                checked_mirror_ids.add(mirror_ota.id)
                mirror_conflict = await _lock_conflict_message(
                    db, obj.object_type_id, mirror_ota.id, object_id=obj.id
                )
                if mirror_conflict is not None and mirror_conflict not in seen_messages:
                    seen_messages.add(mirror_conflict)
                    conflicts.append(mirror_conflict)

    if conflicts:
        raise HTTPException(status_code=409, detail={"errors": conflicts})
    return skip_indices


async def replace_object_values(
    db: AsyncSession, obj: Object, payload: ObjectIn, actor_id: uuid.UUID,
    *, is_superuser: bool,
) -> None:
    """Jira replace semantics (WRIT-02): passed attributes fully replace
    their value sets; unpassed attributes keep theirs; an empty attribute
    set is padded with the label attribute + Object.name (SYNC-PUSH-JIRA-08).
    ``actor_id`` is the caller, whose record visibility applies to reference
    targets (quick 261012-ca3).

    CORESEAM-11/D-14: one trusted public-consumer origin threads through every
    delegated write of this composite request (each value/ref delete and create,
    plus the final Object touch). It derives only from the already-resolved
    public actor and there is deliberately no override parameter, so neither a
    client nor a future caller forwarding untrusted input can choose it
    (T-291.2-13). Per D-07/
    D-08 only the CSV importer aggregates a per-request event — this method
    still emits exactly one hook per real service write.

    Implementation notes:

    - Two ``validate_object_payload`` calls (when padding kicks in): the
      first resolves the label_attr_id (a payload with empty attributes
      has no entries, so we just need the label_attr_id to construct the
      padding entry); the second runs on the PADDED payload so the padding
      label entry becomes part of plan.entries and persists in step 3.
      When the attribute set is non-empty we only run the main pass.

    - Both passes use ``require_label=False`` (D-04: label presence is
      CREATE-only; a PUT without the label attribute keeps the current
      name — WRIT-02 unpassed-keep — and must NOT 400).

    - The label-padding branch constructs
      ``AttributeEntry(objectTypeAttributeId=<label_attr_id>,
      objectAttributeValues=[ObjectAttributeValue(value=obj.name)])``.
      The padding entry persists through the same step-3 write loop as
      any user-supplied entry — no special path.

    - 290 D-02/D-05/D-06: before step 3 the passed entries are classified
      against the persisted state. Entries already equal to the persisted
      value/ref multiset are skipped (no delete/recreate/audit) so a
      full-replace payload that repeats an externally locked value is a
      no-op; every real change is checked against the effective persisted
      lock and the whole request answers a single 409 ``errors`` list BEFORE
      the first delete. Unpassed attributes are untouched either way, and an
      entirely unchanged request never reaches ``update_object`` (no Updated
      system attribute, no audit, no outbox). For a non-superuser only the
      caller-visible references are compared (quick 261012-cpp).

    - Per passed attribute: delete-then-recreate through the SHARED
      value services. Value rows are deleted by
      ``(object_id, object_type_attribute_id)``; ref rows by
      ``(from_object_id, from_attribute_name)`` (refs carry
      from_attribute_name, NOT an OTA id). The new values are created
      via the shared services so audit + outbox + name-sync +
      ``_touch_system_attribute`` fire per the WRIT-04 contract. For a
      non-superuser only caller-visible references are replaced; references
      to objects it may not read are preserved (quick 261012-cpp).

    - 317 D-15/EXT-06-T2 — one unit of work, serialised per object. The
      whole delete/recreate sequence (plus the final touch) rides ONE
      transaction: every shared-service call runs with ``commit=False``,
      ``emit_index_outbox=False`` and ``invalidate_cache=False``, exactly one
      object-level ``IndexOutbox`` row is written ``ENQUEUED`` inside that
      transaction, and the single ``commit`` happens at the end. Only after
      that commit are the one enqueue (``persist_status=False`` — the row is
      already ``ENQUEUED``) and the one cache invalidation performed. The
      per-value audit rows stay per value; only the transaction boundary and
      the outbox/invalidation amplification moved.

    - The same lock also enforces single-valued cardinality. Before the first
      destructive write this method takes ``SELECT ... FOR UPDATE`` on the
      object row, so two concurrent PUTs queue behind each other instead of
      interleaving their delete/recreate loops (that interleaving is what the
      ledger measured as a 500-after-partial-replace, and it is what could
      leave two rows for a ``maximum_cardinality == 1`` attribute). The
      second writer's delete therefore always sees the first writer's
      COMMITTED rows. A partial unique index on
      ``(object_id, object_type_attribute_id)`` for cardinality-1 attributes
      is deliberately NOT added here: it would be a second Alembic revision
      in a phase that must keep the squashed baseline valid, so it stays an
      after-1.0 follow-up.

    - Final object-level touch: ``update_object(db, obj.id, ...)`` fires
      the Updated system attribute + the object-level audit (WRIT-04); its
      outbox row and invalidation are suppressed here because this method
      emits exactly one of each after the single commit (see above). When
      the label attribute was
      among the entries just (re)written, we pass its new value
      explicitly via ``ObjectUpdate(name=plan.label_value)`` —
      ``create_object_value``'s internal ``_sync_object_name`` hook
      only fires for ``attribute.name == "Name"`` (a literal string
      match, NOT ``object_type.label_attribute_id``-based). For object
      types whose label attribute is legitimately named something
      else (Pitfall 6, see 203-RESEARCH.md), Object.name — and the
      response's top-level ``label`` field, which is read directly
      from ``data.obj["name"]`` in object_shape.py:377 — would
      otherwise stay stale. The ``label_passed`` check closes the gap
      explicitly, mirroring what Plan 03's create path does by
      setting ``ObjectCreate(name=plan.label_value, ...)`` directly
      rather than relying on the attribute-name coincidence.
    """
    # CORESEAM-11/D-14: resolve the single trusted origin for this composite
    # request once. The already resolved public consumer actor yields a user
    # origin named after the stable public-API operation (never a
    # synthetic/plugin identity).
    effective_origin = _resolve_origin(
        None, PUBLIC_OBJECT_UPDATE_ORIGIN, actor_id
    )

    # 317 D-15/EXT-06-T2: serialise every replace of this object. The row lock
    # is taken BEFORE the validation reads persisted state and before the first
    # destructive write, so two concurrent PUTs queue behind each other instead
    # of interleaving their delete/recreate loops. This is also what enforces
    # single-valued cardinality without a partial unique index: the second
    # writer's delete/compare always sees the first writer's COMMITTED rows, so
    # a maximum_cardinality == 1 attribute can never end up with two rows. The
    # partial unique index stays an after-1.0 follow-up (it would be a second
    # Alembic revision on top of this phase's squashed baseline — D-10).
    await db.execute(
        text("SELECT id FROM objects WHERE id = :object_id FOR UPDATE"),
        {"object_id": obj.id},
    )

    # 1. Label padding on empty attribute sets (SYNC-PUSH-JIRA-08 mirror:
    #    Jira rejects an empty-attribute PUT with 400 — we pad instead).
    entries = list(payload.attributes)
    padded = False
    if not entries:
        # Pre-pass: just to resolve label_attr_id from the object type.
        # With an empty attributes list the plan.entries will be [] — the
        # only thing we need from this call is label_attr_id.
        label_plan = await validate_object_payload(
            db, payload, obj.object_type_id, require_label=False,
            user_id=str(actor_id), is_superuser=is_superuser,
        )
        if label_plan.label_attr_id is None:
            raise HTTPException(
                status_code=400,
                detail="Object type has no label attribute; empty attribute set cannot be padded",
            )
        entries = [
            AttributeEntry(
                objectTypeAttributeId=label_plan.label_attr_id,
                objectAttributeValues=[ObjectAttributeValue(value=obj.name)],
            )
        ]
        padded = True

    # 2. Validate the payload that will actually be written. The PADDED
    #    payload in the empty-set branch so the padding entry becomes
    #    part of plan.entries and persists in step 3; the ORIGINAL
    #    payload otherwise. require_label=False on both branches:
    #    label presence is CREATE-only (D-04) — a PUT without the
    #    label attribute keeps the current name (WRIT-02 unpassed-keep)
    #    and must NOT 400. objectTypeId mismatch is rejected by the
    #    caller (route).
    write_payload = payload
    if padded:
        write_payload = ObjectIn(objectTypeId=payload.objectTypeId, attributes=entries)
    plan = await validate_object_payload(
        db, write_payload, obj.object_type_id, require_label=False,
        user_id=str(actor_id), is_superuser=is_superuser,
    )

    # 253 Pitfall 7: resolve this object type's system Name OTA once
    # (own-type first, inherited Weg-A parent as fallback — the same order
    # _mirror_system_value and update_object()'s mirror use, so the row that
    # survives below is exactly the one update_object() will keep in sync).
    system_name_result = await db.execute(
        select(ObjectTypeAttribute).where(
            ObjectTypeAttribute.object_type_id == obj.object_type_id,
            ObjectTypeAttribute.name == "Name",
            ObjectTypeAttribute.is_system.is_(True),
        )
    )
    system_name_ota = system_name_result.scalar_one_or_none()
    system_name_ota_id = (
        system_name_ota.id if system_name_ota
        else await resolve_ancestor_ota_by_name(db, "Name", obj.object_type_id)
    )

    # 2b. 290 D-02/D-05/D-06: classify the passed entries against the
    #     persisted state BEFORE the first destructive write. Unchanged
    #     entries are skipped (D-05 no-op) and any real change into a
    #     persisted externally managed value raises the single 409
    #     {"errors": [...]} here — all-or-nothing, no partial commit.
    # quick 261012-cpp: for a non-superuser, references to targets it may not
    # read are neither compared nor deleted. One fixed lookup, independent of
    # whether hidden references exist; None means no filter applies.
    visible_targets = None
    if any(_is_reference_def(entry.attr_def) for entry in plan.entries):
        visible_targets = await load_visible_reference_targets(
            db, [obj.id], str(actor_id), is_superuser=is_superuser
        )
    skip_indices = await _preflight_replace_locks(
        db, obj, plan, system_name_ota_id, visible_targets=visible_targets
    )

    # 3. Per passed attribute: delete-then-recreate through shared services.
    #    Unpassed attributes are never touched (their value rows and ref
    #    rows remain in the DB). Entries already equal to the persisted
    #    state are skipped (2b) — no delete/recreate/audit.
    #
    # 317 D-15/EXT-06-T2: from here on the whole sequence is ONE
    # transaction. Every shared-service call drops its own commit, its own
    # outbox row and its own cache invalidation; the single commit is at the
    # end and the except clause rolls the entire sequence back, so a failed
    # replace leaves the object exactly as it was (never a half-replaced
    # state, which is the failure the ledger measured).
    try:
        for index, entry in enumerate(plan.entries):
            if index in skip_indices:
                continue
            ota_id = uuid.UUID(entry.attr_def["id"])
            attr_name = entry.attr_def["name"]

            # 253 Pitfall 7: the system Name OTA is exempt from delete-then-
            # recreate. Its row mirrors Object.name (D-01) and
            # create_object_value deliberately persists nothing for it, so a
            # delete here would drop the row for good. The
            # label_passed/update_object() call at the end of this function
            # updates the surviving row instead.
            if system_name_ota_id is not None and ota_id == system_name_ota_id:
                continue

            # Delete existing scalar values for this attribute (scoped to
            # this object + this OTA — the WHERE clause prevents touching
            # values for the same OTA on a different object).
            vals = (
                await db.execute(
                    select(ObjectValue).where(
                        ObjectValue.object_id == obj.id,
                        ObjectValue.object_type_attribute_id == ota_id,
                    )
                )
            ).scalars().all()
            for v in vals:
                await delete_object_value(
                    db, v.id, actor_id, origin=effective_origin,
                    commit=False, emit_index_outbox=False, invalidate_cache=False,
                )

            # Delete existing refs for this attribute (matched by name —
            # refs carry from_attribute_name, NOT an OTA id).
            refs = (
                await db.execute(
                    select(ObjectRef).where(
                        ObjectRef.from_object_id == obj.id,
                        ObjectRef.from_attribute_name == attr_name,
                    )
                )
            ).scalars().all()
            for r in refs:
                if visible_targets is not None and str(r.to_object_id) not in visible_targets:
                    continue
                await delete_object_ref(
                    db, r.id, actor_id, origin=effective_origin,
                    commit=False, emit_index_outbox=False, invalidate_cache=False,
                )

            # Recreate from the payload (empty value set → attribute cleared).
            for vop in entry.value_ops:
                vop.object_id = obj.id
                vop.object_type_attribute_id = ota_id
                await create_object_value(
                    db, vop, actor_id, origin=effective_origin,
                    commit=False, emit_index_outbox=False, invalidate_cache=False,
                )
            for rop in entry.ref_ops:
                rop.from_object_id = obj.id
                rop.from_attribute_name = attr_name
                await create_object_ref(
                    db, rop, actor_id, origin=effective_origin,
                    commit=False, emit_index_outbox=False, invalidate_cache=False,
                )

        # 4. Final object-level touch: fires the Updated system attribute +
        #    audit (WRIT-04) on the default path; on this composite path the
        #    outbox row and the invalidation are emitted once below instead
        #    of once per value. When the label attribute was among the
        #    entries just (re)written, pass its new value explicitly — see
        #    the "Name-sync correction" note in the docstring above. An
        #    all-None ObjectUpdate only drives the pipeline otherwise; no
        #    Object.name change happens in that branch.
        #    290 D-05: an entirely unchanged request (every passed entry
        #    already equals the persisted state) is a side-effect-free
        #    no-op — no touch, no outbox row, no audit.
        if len(skip_indices) == len(plan.entries):
            # A bare commit releases the row lock taken above instead of
            # holding it through response assembly. It writes nothing.
            await db.commit()
            return

        label_passed = (
            plan.label_attr_id is not None
            and any(
                str(e.objectTypeAttributeId) == str(plan.label_attr_id)
                for e in write_payload.attributes
            )
        )
        await update_object(
            db, obj.id,
            ObjectUpdate(name=plan.label_value) if label_passed else ObjectUpdate(),
            actor_id,
            origin=effective_origin,
            commit=False, emit_index_outbox=False, invalidate_cache=False,
        )

        # 5. ONE object-level outbox row, written INSIDE the data transaction
        #    and pre-marked ENQUEUED: the enqueue below must not switch it to
        #    ENQUEUED with its own UPDATE + commit (that would be a second
        #    commit). Safe on an enqueue failure because outbox_sweeper_job
        #    reaps rows whose status is PENDING *or* ENQUEUED after the
        #    two-minute cooldown (same reasoning as public_create_object).
        outbox_entry = _new_outbox_entry(
            obj.id, obj.object_type_id, "index",
            actor_id=actor_id, origin=effective_origin,
        )
        outbox_entry.status = IndexOutboxStatus.ENQUEUED.value
        db.add(outbox_entry)
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    # 6. Post-commit: one enqueue for the one outbox row (persist_status=False
    #    — the row is already ENQUEUED), then one purge of the four namespaces
    #    a composite write can change (D-14: the same helper the POST route
    #    calls), never one per value.
    await _enqueue_index_job(
        db, obj.id, "index_object_job",
        outbox_id=outbox_entry.id, persist_status=False,
    )
    await invalidate_composite_write_caches()

