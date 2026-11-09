"""CSV import execution orchestration.

Queries the ImportDefinition + attribute metadata, reads the uploaded CSV file,
delegates parsing to the pure CsvImportConnector, then upserts Objects and
ObjectValues based on the identity attribute matching logic.

Mirrors Phase 155's export_execution_service.py pattern.
"""

import json
import logging
import os
import uuid
from collections import Counter
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.attribute_type import AttributeTypeDataType
from app.models.import_definition import ImportDefinition
from app.models.object import Object
from app.models.object_ref import ObjectRef
from app.models.object_type import ObjectType
from app.models.object_type_attribute import ObjectTypeAttribute
from app.models.object_value import ObjectValue
from app.models.schema import Schema
from app.models.status import Status
from app.models.task import Task
from app.models.user import User
from app.services.connectors.csv_connector import CsvImportConnector, CsvImportResult, detect_delimiter
from app.services.hook_origin import MutationOrigin, import_origin
from app.services.import_service import update_import
from app.services.object_service import (
    _new_outbox_entry,
    _enqueue_index_job,
    _fire_object_hook,
)
from app.services.object_value_service import (
    _as_naive_utc,
    _validate_and_sanitize_textarea_value_for_type,
)
from app.services import value_lock_service
from app.services.cache_service import invalidate_entity_cache
from app.schemas.crud import ImportUpdate
from app.services.audit_service import audit_log, model_to_dict
from app.services.translation_service import delete_translations_for
from app.services.typed_value_mapping import typed_column_for_data_type
from app.services import task_service
from app.services.auth_service import resolve_user_permissions
from app.services.record_permission_service import resolve_record_permission

task_logger = logging.getLogger("task.import")


def _typed_column_for_datatype(dt: str) -> str:
    """Map data type string to ObjectValue typed column name.

    D-09 CSV wire-format carve-out: STATUS and JSON keep an explicit
    value_simple branch here instead of delegating to the seam (which maps
    both to value_struct) — the CSV import wire format writes status ids and
    raw JSON strings into value_simple via CsvImportConnector._coerce_value,
    and _extract_typed_value (below) plus the identity-match query in
    _execute_csv_import_body read that same wire column. Delegating STATUS/
    JSON to typed_column_for_data_type here would silently break STATUS/JSON
    identity matching on import. Every other data type delegates to the
    canonical seam (D-08).
    """
    if dt in (AttributeTypeDataType.STATUS.value, AttributeTypeDataType.JSON.value):
        return "value_simple"
    return typed_column_for_data_type(dt)


def _extract_typed_value(entry: dict, data_type: str) -> Any:
    """Extract the non-None typed value from a coerced CsvImportConnector entry.

    The entry dict returned by _coerce_value has keys value_simple, value_integer,
    value_float, value_boolean, value_datetime — only one is populated per the
    attribute's data_type. Returns the value (or None if the entry supplies no
    value for the column corresponding to data_type).
    """
    if entry is None:
        return None
    col = _typed_column_for_datatype(data_type)
    return entry.get(col)


_ERROR_MESSAGE_MAX_LEN = 1990  # ImportUpdate.error_message has max_length=2000


def _build_error_message_json(error_report: list[dict]) -> str | None:
    """Serialize error_report to JSON, truncating the *list* (not the string) so
    the result is always valid, parseable JSON within _ERROR_MESSAGE_MAX_LEN chars.

    WR-08: naively slicing the json.dumps() string mid-structure never produces
    valid JSON. Instead, drop trailing entries and append a summary sentinel
    until the serialized list fits the length budget.
    """
    if not error_report:
        return None

    full = json.dumps(error_report)
    if len(full) <= _ERROR_MESSAGE_MAX_LEN:
        return full

    kept = list(error_report)
    while kept:
        kept.pop()
        dropped = len(error_report) - len(kept)
        sentinel = {"row_number": None, "reason": f"{dropped} more error(s) truncated"}
        candidate = json.dumps(kept + [sentinel])
        if len(candidate) <= _ERROR_MESSAGE_MAX_LEN:
            return candidate

    # Pathological case: even a single-entry list + sentinel doesn't fit.
    return json.dumps([{"row_number": None, "reason": "error report truncated"}])


async def _write_object_values(
    db: AsyncSession,
    object_id: uuid.UUID,
    attr_id: uuid.UUID,
    entry: dict,
    actor_id: uuid.UUID | None = None,
    data_type: str | None = None,
    structure: dict | None = None,
) -> None:
    """Insert ObjectValue rows for a single parsed entry.

    Handles single-value and multi-value entries.  Does NOT handle reference
    entries (those write ObjectRef rows instead via _write_object_refs).

    Single-value entries are plain dicts with typed value keys.
    Multi-value entries have a ``"values"`` key containing a list of such dicts.

    `data_type` and `structure` come from the preloaded plain-value caches
    (type_cache / attr_ref_cache) so the textarea write invariants can be
    enforced without touching possibly-expired ORM instances (WR-03 rollback
    hazard) — see _validate_and_sanitize_textarea_value_for_type (WR-01).
    """
    if entry is None:
        return
    if "ref_object_ids" in entry:
        return  # handled by _write_object_refs

    raw_attr_id: str | uuid.UUID = entry.get("attr_id", str(attr_id))
    parsed_attr_id = uuid.UUID(raw_attr_id) if isinstance(raw_attr_id, str) else raw_attr_id

    values_list: list[dict] = entry.get("values") if isinstance(entry.get("values"), list) else [entry]

    for val_dict in values_list:
        # WR-01: CSV import builds ObjectValue ORM rows directly, so it must
        # apply the same write invariants as the API service — non-textarea
        # values stay capped at 5,000 chars, textarea at 64,000, and
        # format='html' textarea values pass through the Bleach allow-list
        # layer before persistence. Raises ValueError -> the per-row WR-03
        # handler records it as a row-level error and the row is rolled back.
        sanitized = _validate_and_sanitize_textarea_value_for_type(
            data_type, structure, val_dict.get("value_simple")
        )
        if sanitized is not None:
            val_dict = dict(val_dict)
            val_dict["value_simple"] = sanitized
        ov = ObjectValue(
            id=uuid.uuid4(),
            object_id=object_id,
            object_type_attribute_id=parsed_attr_id,
            value_simple=val_dict.get("value_simple"),
            value_integer=val_dict.get("value_integer"),
            value_float=val_dict.get("value_float"),
            value_boolean=val_dict.get("value_boolean"),
            value_datetime=val_dict.get("value_datetime"),
        )
        db.add(ov)
        await db.flush()
        await audit_log(db, "object_values", ov.id, "CREATE", actor_id,
                        old_values=None, new_values=model_to_dict(ov), object_id=object_id,
                        object_type_attribute_id=parsed_attr_id)


def _normalized_scalar_entries(
    entry: dict, data_type: str | None, structure: dict | None
) -> list[tuple]:
    """Normalize a coerced entry into the typed tuples its ObjectValue rows persist.

    Mirrors _write_object_values' sanitization exactly (same textarea/HTML rules
    and length caps) so an unchanged-value comparison is against the canonical
    persisted shape, never the raw CSV wire value.
    """
    values_list: list[dict] = (
        entry.get("values") if isinstance(entry.get("values"), list) else [entry]
    )
    normalized: list[tuple] = []
    for val_dict in values_list:
        sanitized = _validate_and_sanitize_textarea_value_for_type(
            data_type, structure, val_dict.get("value_simple")
        )
        value_simple = sanitized if sanitized is not None else val_dict.get("value_simple")
        normalized.append((
            value_simple,
            val_dict.get("value_integer"),
            val_dict.get("value_float"),
            val_dict.get("value_boolean"),
            _as_naive_utc(val_dict.get("value_datetime")),
        ))
    return normalized


def _persisted_scalar_entries(object_values: list[ObjectValue]) -> list[tuple]:
    """The persisted ObjectValue rows in the same normalized tuple shape."""
    return [
        (
            ov.value_simple,
            ov.value_integer,
            ov.value_float,
            ov.value_boolean,
            _as_naive_utc(ov.value_datetime),
        )
        for ov in object_values
    ]


def _normalized_ref_ids(entry: dict) -> list[uuid.UUID]:
    """Resolved target UUIDs an entry would persist (invalid values skipped)."""
    ref_ids: list[uuid.UUID] = []
    for raw in entry.get("ref_object_ids") or []:
        try:
            ref_ids.append(uuid.UUID(str(raw)))
        except (ValueError, TypeError):
            continue
    return ref_ids


async def _skip_noop_attrs(
    db: AsyncSession,
    *,
    object_type_id: uuid.UUID,
    object_id: uuid.UUID | None,
    object_obj: Object | None,
    row_data: dict,
    attr_ref_cache: dict,
    type_cache: dict,
) -> set[uuid.UUID]:
    """D-02/D-05/D-07/D-08 per-row preflight: skip EVERY unchanged attribute.

    Every mapped entry is compared against the persisted, normalized content
    first (scalar Counter incl. the Name label, ref-target Counter). An entry
    whose content is already identical is returned as a skip — no delete/insert,
    no audit, no name mirror, no outbox churn and no hook. This is what makes a
    re-imported no-op row side-effect-free (D-08) while still leaving real
    changes to be written.

    Only a REAL change reaches the lock check: a locked real change raises
    ValueLockConflict, which the row loop turns into a row error and a rollback.
    Lock reads come fresh from value_lock_service's persisted ``SELECT ...
    FOR SHARE`` reads taken inside this row's transaction, so they are held
    through this row's commit and never reused across rows.
    """
    skip_attrs: set[uuid.UUID] = set()

    # 290 D-02: the identity match is not type-scoped — an inherited identity
    # OTA can match Objects of a descendant ObjectType. The lock owner is
    # therefore the matched Object's own type (fresh persisted read), not the
    # import definition's type; a new Object uses the definition's type.
    lock_owner_type_id = (
        object_obj.object_type_id if object_obj is not None else object_type_id
    )

    for attr_id_str, entry in row_data.items():
        if entry is None:
            continue
        attr_id = attr_id_str if isinstance(attr_id_str, uuid.UUID) else uuid.UUID(attr_id_str)
        attr_ref = attr_ref_cache.get(attr_id) or {}
        attr_name = attr_ref.get("name") or ""
        data_type = type_cache.get(attr_id)
        structure = attr_ref.get("structure")

        if isinstance(entry, dict) and "ref_object_ids" in entry:
            # D-02/D-03/D-05: reference binding preflight — the source object's
            # own OT OR the resolved attribute by name locks the write; the
            # normalized target multiset decides whether it is a real change.
            incoming_refs = Counter(_normalized_ref_ids(entry))
            persisted_refs: Counter = Counter()
            if object_id is not None:
                existing_refs = (await db.execute(
                    select(ObjectRef).where(
                        ObjectRef.from_object_id == object_id,
                        ObjectRef.from_attribute_name == attr_name,
                    )
                )).scalars().all()
                persisted_refs = Counter(ref.to_object_id for ref in existing_refs)

            if incoming_refs == persisted_refs:
                skip_attrs.add(attr_id)
            else:
                await value_lock_service.assert_binding_writable(
                    db,
                    object_type_id=lock_owner_type_id,
                    attribute_name=attr_name,
                    object_id=object_id,
                )
            continue

        incoming_values = Counter(_normalized_scalar_entries(entry, data_type, structure))
        persisted_values: Counter = Counter()
        if object_id is not None:
            persisted_ovs = (await db.execute(
                select(ObjectValue).where(
                    ObjectValue.object_id == object_id,
                    ObjectValue.object_type_attribute_id == attr_id,
                )
            )).scalars().all()
            persisted_values = Counter(_persisted_scalar_entries(persisted_ovs))

        unchanged = incoming_values == persisted_values
        if unchanged and attr_name == "Name" and object_obj is not None:
            new_name = _extract_typed_value(entry, data_type)
            if new_name:
                unchanged = object_obj.name == str(new_name)

        if unchanged:
            skip_attrs.add(attr_id)
        else:
            await value_lock_service.assert_value_writable(
                db,
                object_type_id=lock_owner_type_id,
                attribute_id=attr_id,
                object_id=object_id,
            )

    return skip_attrs


async def _resolve_import_principal(
    db: AsyncSession,
    task_id: uuid.UUID | None,
    principal_id: uuid.UUID | None,
) -> uuid.UUID:
    """The user whose write permissions the import runs under — fail closed.

    TRI317-49: mirrors the export's principal resolution (317-08). The worker
    path takes ``Task.creator_id``; a creatorless task is refused instead of
    importing with no permission check at all.
    """
    if principal_id is not None:
        return principal_id
    if task_id is not None:
        creator_id = (
            await db.execute(select(Task.creator_id).where(Task.id == task_id))
        ).scalar_one_or_none()
        if creator_id is not None:
            return creator_id
        raise ValueError(
            f"Import task {task_id} has no creator; refusing to import without "
            "a resolvable principal"
        )
    raise ValueError(
        "Import requires a resolvable principal: pass principal_id or a task_id "
        "whose creator is set"
    )


async def _resolve_import_access(
    db: AsyncSession,
    principal_id: uuid.UUID,
    object_type_id: uuid.UUID,
    attr_cache: dict[uuid.UUID, ObjectTypeAttribute],
) -> dict | None:
    """What the principal may write, or ``None`` for a superuser (RLR-05).

    The same gates as the REST write routes: ``objects:create``/``objects:update``
    at type level, a record DENY on the ObjectType (cascading to Schema and
    Source) for creates, and a record DENY on an attribute's AttributeType per
    action (``write`` covers both, D-08). Per-object DENYs are resolved per row.
    """
    user = (
        await db.execute(select(User).where(User.id == principal_id))
    ).scalar_one_or_none()
    if user is None:
        raise ValueError(f"Import principal {principal_id} does not exist")
    if user.is_superuser:
        return None

    user_id = str(user.id)
    permissions = await resolve_user_permissions(user_id)
    can_create = "objects:create" in permissions and await resolve_record_permission(
        user_id, "object_type", object_type_id, "create"
    ) is not False
    can_update = "objects:update" in permissions
    if not can_create and not can_update:
        raise ValueError("Import principal may not create or update objects of this ObjectType")

    denied: dict[str, set[uuid.UUID]] = {"create": set(), "update": set()}
    for attr_id, attr in attr_cache.items():
        if attr.attribute_type_id is None:
            continue
        for action, denied_ids in denied.items():
            decision = await resolve_record_permission(
                user_id, "attribute_type", attr.attribute_type_id, action
            )
            if decision is False:
                denied_ids.add(attr_id)
    return {
        "user_id": user_id,
        "can_create": can_create,
        "can_update": can_update,
        "denied": denied,
    }


async def _may_update_object(access: dict, object_id: uuid.UUID) -> bool:
    if not access["can_update"]:
        return False
    for action in ("read", "update"):
        if await resolve_record_permission(
            access["user_id"], "object", object_id, action
        ) is False:
            return False
    return True


async def execute_csv_import(
    db: AsyncSession,
    import_definition_id: uuid.UUID,
    import_id: uuid.UUID,
    temp_file_path: str,
    actor_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    principal_id: uuid.UUID | None = None,
) -> dict:
    """Orchestrate a CSV import execution — thin wrapper guaranteeing temp-file cleanup.

    Delegates to `_execute_csv_import_body` for the actual steps, but wraps it in
    try/finally so the uploaded CSV temp file is always removed (WR-01) — even if
    Step 1 raises ValueError (missing definition/mappings/identity attr) or the
    per-row loop raises an unexpected exception (e.g. IntegrityError) with no
    surrounding try/finally of its own.

    `task_id` (Phase 247, D-12/D-13) is optional: when supplied, real parsed/
    successful-row counts are published onto the Task via
    `task_service.update_task_progress` — the caller's own Import counters
    are unaffected either way.
    """
    try:
        return await _execute_csv_import_body(
            db, import_definition_id, import_id, temp_file_path, actor_id=actor_id, task_id=task_id,
            principal_id=principal_id,
        )
    finally:
        try:
            os.unlink(temp_file_path)
        except OSError:
            pass


async def _execute_csv_import_body(
    db: AsyncSession,
    import_definition_id: uuid.UUID,
    import_id: uuid.UUID,
    temp_file_path: str,
    actor_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    principal_id: uuid.UUID | None = None,
) -> dict:
    """Orchestrate a CSV import execution.

    1. Load ImportDefinition + field_mappings
    2. Load attribute metadata and type caches
    3. Read CSV content from temp file
    4. Delegate to CsvImportConnector.parse_csv() for parsing/coercion
    5. For each parsed row: identity match -> create or update Object
    6. Write ObjectValues for attributes in field_mappings only
    7. Build error report as JSON array in Import.error_message
    8. Update Import status with counts
    9. Return result dict with row counts and error info (temp file cleanup is
       handled by the `execute_csv_import` wrapper's finally block)
    """
    # ── Step 1: Load ImportDefinition ──────────────────────────────────
    result = await db.execute(
        select(ImportDefinition).where(ImportDefinition.id == import_definition_id)
    )
    definition = result.scalar_one_or_none()
    if not definition:
        raise ValueError(f"ImportDefinition {import_definition_id} not found")

    if not definition.field_mappings:
        raise ValueError(f"ImportDefinition {import_definition_id} has no field_mappings")

    if not definition.identity_attr_id:
        raise ValueError(f"ImportDefinition {import_definition_id} has no identity_attr_id")

    # ── Step 2: Preload caches ────────────────────────────────────────
    # object_type_id is captured as a plain value up front (mirrors identity_attr_id/
    # schema_id/schema_source_id below) rather than read off `definition` inside the
    # per-row loop: this session has expire_on_commit=False (db/__init__.py), but
    # AsyncSession.rollback() unconditionally expires every ORM object already loaded
    # in the session regardless of that setting — and the per-row WR-03 exception
    # handler (5c/5d below) calls `await db.rollback()` on any row-level failure. The
    # row *after* a failed one then hits a bare `definition.object_type_id` attribute
    # access on a now-expired instance, which is a synchronous lazy-load attempt
    # outside the greenlet SQLAlchemy's async engine expects IO to happen in, raising
    # `MissingGreenlet: greenlet_spawn has not been called`.
    object_type_id = definition.object_type_id

    # 2a: ObjectType + Schema (for new Object creation + ObjectRef source)
    result = await db.execute(
        select(ObjectType).where(ObjectType.id == object_type_id)
    )
    object_type = result.scalar_one_or_none()
    if not object_type:
        raise ValueError(f"ObjectType {object_type_id} not found for ImportDefinition")

    schema_id = object_type.schema_id

    result = await db.execute(
        select(Schema).where(Schema.id == schema_id)
    )
    schema_obj = result.scalar_one_or_none()
    schema_source_id: uuid.UUID | None = schema_obj.source_id if schema_obj else None

    # 2b: Attribute cache + type cache
    attr_ids: set[uuid.UUID] = set()
    for mapping in definition.field_mappings:
        raw = mapping["attr_id"]
        attr_ids.add(uuid.UUID(raw) if isinstance(raw, str) else raw)

    attr_ids.add(definition.identity_attr_id)

    attr_cache: dict[uuid.UUID, ObjectTypeAttribute] = {}
    type_cache: dict[uuid.UUID, str] = {}
    # Plain-value companion cache for the ObjectTypeAttribute columns read
    # inside the per-row loop (name, referenced_object_type_id, structure) —
    # same rollback-expiration hazard as object_type_id above: attr_cache
    # holds ORM instances that get expired by the WR-03 per-row
    # `await db.rollback()`, so their attributes can't be read via bare
    # attribute access from later iterations.
    attr_ref_cache: dict[uuid.UUID, dict] = {}
    if attr_ids:
        result = await db.execute(
            select(ObjectTypeAttribute)
            .options(selectinload(ObjectTypeAttribute.attribute_type))
            .where(ObjectTypeAttribute.id.in_(list(attr_ids)))
        )
        for attr in result.scalars().all():
            attr_cache[attr.id] = attr
            attr_ref_cache[attr.id] = {
                "name": attr.name,
                "referenced_object_type_id": attr.referenced_object_type_id,
                "structure": attr.structure,
            }
            if attr.attribute_type:
                dt = attr.attribute_type.data_type
                type_cache[attr.id] = str(dt.value) if hasattr(dt, "value") else str(dt)

    # TRI317-49: the import writes with the task creator's permissions, never
    # with the worker's synthetic system user. Resolved before the row loop so
    # no per-row rollback can expire the ORM rows read here.
    access = await _resolve_import_access(
        db,
        await _resolve_import_principal(db, task_id, principal_id),
        object_type_id,
        attr_cache,
    )
    reported_denied_attrs: set[uuid.UUID] = set()

    # 2c: obj_scope — all Objects of the target type (for name-based reference resolution)
    obj_scope: dict[uuid.UUID, str] = {}
    objs_result = await db.execute(
        select(Object).where(Object.object_type_id == object_type_id)
    )
    for obj_entry in objs_result.scalars().all():
        obj_scope[obj_entry.id] = obj_entry.name

    # 2d: status_cache — name -> id for STATUS-typed attributes (WR-11). Without
    # this, CsvImportConnector._coerce_value's STATUS branch falls through to
    # storing the raw CSV string (e.g. "Active") instead of the matching Status
    # UUID, since `if status_cache:` treats {} as falsy and always takes the
    # else branch.
    status_cache: dict[str, str] = {}
    if schema_id:
        statuses_result = await db.execute(
            select(Status).where(Status.schema_id == schema_id)
        )
        for status_entry in statuses_result.scalars().all():
            status_cache[status_entry.name] = str(status_entry.id)

    # 2e: select_cache — attr_id (str) -> {option value -> option id} for
    # SELECT-typed attributes (mirrors status_cache; D-05: import cells carry
    # display strings, stored values carry ids). Scoped per attribute because
    # the same option value can map to different ids in different attributes.
    # Options without ids (pre-migration rows) are skipped.
    select_cache: dict[str, dict[str, str]] = {}
    for attr_id, attr in attr_cache.items():
        if type_cache.get(attr_id) == AttributeTypeDataType.SELECT.value:
            # WR-02: first-wins on duplicate display values, matching
            # resolve_select_literal / the alembic migration's contract —
            # a plain dict comprehension would keep the LAST duplicate.
            option_map: dict[str, str] = {}
            for opt in (attr.structure or {}).get("options", []):
                if (
                    opt.get("id")
                    and opt.get("value") is not None
                    and opt["value"] not in option_map
                ):
                    option_map[opt["value"]] = str(opt["id"])
            if option_map:
                select_cache[str(attr_id)] = option_map

    # 2f: user_cache/group_cache — username/name -> str(uuid) for USER/GROUP-
    # typed attributes (D-16, mirrors status_cache's shape). Only built when
    # the import's own field_mappings actually reference a USER/GROUP
    # attribute — a name absent from these caches raises a row-level error
    # in CsvImportConnector rather than writing null or a fabricated UUID.
    user_cache: dict[str, str] = {}
    group_cache: dict[str, str] = {}
    if any(dt == AttributeTypeDataType.USER.value for dt in type_cache.values()):
        from app.models.user import User
        users_result = await db.execute(select(User))
        for u in users_result.scalars().all():
            user_cache[u.username] = str(u.id)
    if any(dt == AttributeTypeDataType.GROUP.value for dt in type_cache.values()):
        from app.models.group import Group
        groups_result = await db.execute(select(Group))
        for g in groups_result.scalars().all():
            group_cache[g.name] = str(g.id)

    # ── Step 3: Read CSV from temp file ────────────────────────────────
    try:
        with open(temp_file_path, "r", encoding=definition.encoding or "utf-8") as f:
            csv_content = f.read()
    except (FileNotFoundError, IOError) as e:
        raise ValueError(f"Cannot read CSV temp file {temp_file_path}: {e}")

    # ── Step 4: Delegate to CsvImportConnector ─────────────────────────
    connector = CsvImportConnector(
        delimiter=detect_delimiter(csv_content),
        value_separator=definition.value_separator,
        date_format=definition.date_format,
    )
    parse_result: CsvImportResult = connector.parse_csv(
        csv_content=csv_content,
        field_mappings=definition.field_mappings,
        attr_cache=attr_cache,
        type_cache=type_cache,
        status_cache=status_cache,
        select_cache=select_cache,
        obj_scope=obj_scope,
        user_cache=user_cache,
        group_cache=group_cache,
    )

    # Phase 247 (D-12): the real parsed row count is the honest CSV import
    # total — publish it as soon as it is known, before the per-row loop.
    if task_id is not None:
        await task_service.update_task_progress(db, task_id, total_objects=parse_result.row_count)

    # ── Step 5: Process each row ──────────────────────────────────────
    identity_attr_id = definition.identity_attr_id
    identity_data_type = type_cache.get(identity_attr_id)
    if not identity_data_type:
        raise ValueError(
            f"Identity attribute {identity_attr_id} not found in type_cache — "
            "check that the attribute exists"
        )

    successful_rows = 0
    error_report: list[dict] = []
    identity_attr_id_str = str(identity_attr_id)

    # CORESEAM-11 / D-10/D-10a: copy the import definition's name, the concrete
    # task id and the persisted Task creator into plain scalars BEFORE the first
    # per-row rollback. AsyncSession.rollback() expires every ORM object loaded
    # in this session (see object_type_id above), so neither `definition.name`
    # nor a later `Task.creator_id` read can be tolerated inside the row loop.
    # The hook actor is the persisted Task.creator_id (never the synthetic
    # worker/system user passed as `actor_id`); audit_log keeps the passed
    # `actor_id` unchanged. No Task / a creatorless Task => actor_id=None.
    hook_actor_id: uuid.UUID | None = None
    if task_id is not None:
        hook_actor_id = (await db.execute(
            select(Task.creator_id).where(Task.id == task_id)
        )).scalar_one_or_none()
    import_mutation_origin: MutationOrigin = import_origin(
        definition.name, task_id
    )

    for row_index, row_data in enumerate(parse_result.rows):
        # WR-03: any unexpected exception while resolving or writing this row
        # (identity resolution, lock conflicts, e.g. an IntegrityError from a DB
        # constraint violation on a single bad row) must not abort the rest of
        # the CSV — roll back just this row and record it as a row-level error,
        # matching the feature's "report row-level errors, keep going" contract.
        try:
            # 5a: Extract identity value
            identity_entry = row_data.get(identity_attr_id_str)
            if not identity_entry:
                error_report.append({
                    "row_number": row_index + 1,
                    "reason": "Missing identity attribute value",
                })
                continue

            # Multi-valued identity attr is inherently ambiguous
            if "values" in identity_entry:
                error_report.append({
                    "row_number": row_index + 1,
                    "reason": "Identity attribute is multi-valued — ambiguous",
                })
                continue

            identity_value = _extract_typed_value(identity_entry, identity_data_type)
            if identity_value is None:
                error_report.append({
                    "row_number": row_index + 1,
                    "reason": "Missing identity attribute value",
                })
                continue

            # WR-04: take a transaction-scoped Postgres advisory lock keyed on the
            # identity value before the match-or-create sequence, closing the TOCTOU
            # window between the SELECT below and the Object insert further down —
            # two concurrent imports (or an import racing a manual create) referencing
            # the same identity value now serialize instead of both observing "no
            # match" and creating duplicate Objects. Released automatically at the
            # per-row db.commit()/db.rollback() that already brackets this row.
            lock_key = f"{object_type_id}:{identity_attr_id}:{identity_value}"
            await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"), {"lock_key": lock_key})

            # 5b: Identity match query — use the correct typed column.
            typed_col_name = _typed_column_for_datatype(identity_data_type)
            typed_col = getattr(ObjectValue, typed_col_name)
            identity_conditions = [
                ObjectValue.object_type_attribute_id == identity_attr_id,
                typed_col == identity_value,
            ]
            if typed_col_name == "value_simple":
                # Text identities are additionally matched by md5 so the lookup
                # can use idx_object_values_attr_text_md5. A plain B-tree on
                # value_simple would reject TEXTAREA values above the ~2,704-byte
                # key limit. The exact equality stays as a recheck: the md5
                # predicate drives the index, and a theoretical collision can
                # never match the wrong Object.
                identity_conditions.append(
                    func.md5(typed_col) == func.md5(str(identity_value))
                )

            ov_result = await db.execute(
                select(ObjectValue).where(*identity_conditions)
            )
            matching_ovs = ov_result.scalars().all()

            if len(matching_ovs) > 1:
                # D-07: Ambiguous — skip
                error_report.append({
                    "row_number": row_index + 1,
                    "reason": f"Ambiguous identity match: {len(matching_ovs)} objects share "
                             f"identity value '{identity_value}'",
                })
                continue

            # Resolve the row's target Object before the first possible mutation.
            if len(matching_ovs) == 0:
                object_obj = None
                object_id = None
            else:
                object_id = matching_ovs[0].object_id
                object_obj = await db.get(Object, object_id)

            # TRI317-49: the principal's write permissions, before any mutation.
            if access is not None:
                action = "create" if object_id is None else "update"
                if action == "create" and not access["can_create"]:
                    reason = "Not permitted to create objects of this ObjectType"
                elif action == "update" and not await _may_update_object(access, object_id):
                    reason = "Not permitted to modify the matched object"
                elif identity_attr_id in access["denied"][action]:
                    reason = "Not permitted to write the identity attribute"
                else:
                    reason = None
                if reason is not None:
                    error_report.append({"row_number": row_index + 1, "reason": reason})
                    continue
                denied_attrs = access["denied"][action]
                if denied_attrs:
                    kept: dict = {}
                    for aid, entry in row_data.items():
                        attr_uuid = aid if isinstance(aid, uuid.UUID) else uuid.UUID(aid)
                        if attr_uuid not in denied_attrs:
                            kept[aid] = entry
                        elif attr_uuid not in reported_denied_attrs:
                            reported_denied_attrs.add(attr_uuid)
                            error_report.append({
                                "row_number": row_index + 1,
                                "reason": f"Attribute '{attr_ref_cache.get(attr_uuid, {}).get('name', attr_uuid)}' "
                                          "not written: permission denied",
                            })
                    row_data = kept

            # 5c: Per-row persisted preflight (290 D-02/D-05/D-07, 291.2 D-08) —
            # immediately before the row's first possible mutation, resolve the
            # effective lock from fresh persisted markers inside this row's
            # transaction and mark every unchanged attribute as a skip. Unchanged
            # attributes are left untouched; a locked real change raises and
            # rolls back this row through the handler below.
            skip_attrs = await _skip_noop_attrs(
                db,
                object_type_id=object_type_id,
                object_id=object_id,
                object_obj=object_obj,
                row_data=row_data,
                attr_ref_cache=attr_ref_cache,
                type_cache=type_cache,
            )

            # ── 5c2: Whole-row no-op + old snapshots ───────────────────
            # A matched row whose every supplied entry is unchanged (and whose
            # label is unchanged — enforced inside _skip_noop_attrs) has nothing
            # to write: no replacement rows, no audit, no outbox row and no
            # hook. It is still a successfully processed row (D-08).
            non_none_entry_ids = {
                aid if isinstance(aid, uuid.UUID) else uuid.UUID(aid)
                for aid, e in row_data.items()
                if e is not None
            }
            is_new_object = len(matching_ovs) == 0
            if not is_new_object and non_none_entry_ids.issubset(skip_attrs):
                await db.commit()
                successful_rows += 1
                if task_id is not None:
                    await task_service.update_task_progress(
                        db, task_id, processed_objects=successful_rows
                    )
                continue

            # D-14: capture the old Object snapshot before the first mutation of
            # this row so on_object_updated can report old/new Object columns.
            old_obj_snapshot = (
                model_to_dict(object_obj) if object_obj is not None else None
            )
            old_value_snapshots: dict[uuid.UUID, list[dict]] = {}

            # ── 5d: Create or update Object ─────────────────────────────
            if is_new_object:
                # D-08: No match — create new Object
                object_obj = Object(
                    id=uuid.uuid4(),
                    schema_id=schema_id,
                    object_type_id=object_type_id,
                    name=str(identity_value),
                )
                db.add(object_obj)
                await db.flush()
                await audit_log(db, "objects", object_obj.id, "CREATE", actor_id,
                                old_values=None, new_values=model_to_dict(object_obj), object_id=object_obj.id)
                object_id = object_obj.id
            else:
                # D-09: Match found — update existing Object.
                # Object.name (the label) IS mutable here — see the "Name" attribute
                # handling in the 5e loop below, which mirrors object_value_service
                # ._sync_object_name's rule that the "Name" attribute drives the label.
                # Delete existing ObjectValues for attributes present in this row
                # (unchanged attributes are skipped so their persisted rows keep
                # their id, value and audit — D-08). The per-attribute old rows are
                # captured first, for the D-14 attribute_changes payload.
                for attr_id_str in row_data:
                    attr_id = attr_id_str if isinstance(attr_id_str, uuid.UUID) else uuid.UUID(attr_id_str)
                    entry = row_data[attr_id_str]
                    if attr_id in skip_attrs or entry is None:
                        continue
                    if isinstance(entry, dict) and "ref_object_ids" in entry:
                        # Reference rows are ObjectRef, not ObjectValue — handled
                        # in 5e.
                        continue
                    del_result = await db.execute(
                        select(ObjectValue).where(
                            ObjectValue.object_id == object_id,
                            ObjectValue.object_type_attribute_id == attr_id,
                        )
                    )
                    ovs_to_del = del_result.scalars().all()
                    old_value_snapshots[attr_id] = [
                        model_to_dict(ov) for ov in ovs_to_del
                    ]
                    for ov_to_del in ovs_to_del:
                        await audit_log(db, "object_values", ov_to_del.id, "DELETE", actor_id,
                                        old_values=model_to_dict(ov_to_del), new_values=None, object_id=object_id,
                                        object_type_attribute_id=attr_id)
                        await db.delete(ov_to_del)
                    await delete_translations_for(db, "object_value", [ov.id for ov in ovs_to_del])

            # ── 5e: Write ObjectValues for all attributes in this row ──
            for attr_id_str, entry in row_data.items():
                attr_id = attr_id_str if isinstance(attr_id_str, uuid.UUID) else uuid.UUID(attr_id_str)
                if attr_id in skip_attrs:
                    # Unchanged attribute: the persisted row keeps its id, value
                    # and audit — no delete/rewrite churn (D-08).
                    continue
                attr_ref = attr_ref_cache.get(attr_id)
                data_type = type_cache.get(attr_id)

                if entry is None:
                    continue

                # Handle references separately
                if isinstance(entry, dict) and "ref_object_ids" in entry:
                    # D-09: apply only the actual multiset delta over resolved
                    # target UUIDs — unchanged bindings keep their ObjectRef id
                    # and audit, only surplus rows are deleted and only missing
                    # targets are inserted. Each real delta emits its ref-specific
                    # hook (never a general on_object_updated, D-03) in this row's
                    # transaction, before its commit (D-04/D-05).
                    attr_name = attr_ref["name"] if attr_ref else ""
                    existing_refs = (await db.execute(
                        select(ObjectRef).where(
                            ObjectRef.from_object_id == object_id,
                            ObjectRef.from_attribute_name == attr_name,
                        )
                    )).scalars().all()
                    existing_by_target: dict[uuid.UUID, list[ObjectRef]] = {}
                    for existing_ref in existing_refs:
                        existing_by_target.setdefault(
                            existing_ref.to_object_id, []
                        ).append(existing_ref)
                    incoming_counts = Counter(_normalized_ref_ids(entry))

                    for target_id, target_rows in existing_by_target.items():
                        keep = incoming_counts.get(target_id, 0)
                        for ref_to_del in target_rows[keep:]:
                            old_snapshot = model_to_dict(ref_to_del)
                            await _fire_object_hook(
                                "on_object_ref_deleted",
                                db,
                                object_id=object_id,
                                schema_id=object_obj.schema_id,
                                object_type_id=object_obj.object_type_id,
                                ref_id=ref_to_del.id,
                                old_values=old_snapshot,
                                new_values=None,
                                actor_id=hook_actor_id,
                                origin=import_mutation_origin.as_payload(),
                            )
                            await db.delete(ref_to_del)

                    ref_type_id = (
                        attr_ref["referenced_object_type_id"]
                        if attr_ref and attr_ref["referenced_object_type_id"]
                        else object_type_id
                    )
                    for target_id, incoming_count in incoming_counts.items():
                        missing = incoming_count - len(
                            existing_by_target.get(target_id, [])
                        )
                        for _ in range(max(missing, 0)):
                            object_ref = ObjectRef(
                                id=uuid.uuid4(),
                                from_object_id=object_id,
                                to_object_id=target_id,
                                object_type_id=ref_type_id,
                                source_id=schema_source_id or uuid.UUID("00000000-0000-0000-0000-000000000000"),
                                from_attribute_name=attr_name,
                            )
                            db.add(object_ref)
                            new_snapshot = model_to_dict(object_ref)
                            await audit_log(db, "object_refs", object_ref.id, "CREATE", actor_id,
                                            old_values=None, new_values=new_snapshot, object_id=object_id)
                            await _fire_object_hook(
                                "on_object_ref_created",
                                db,
                                object_id=object_id,
                                schema_id=object_obj.schema_id,
                                object_type_id=object_obj.object_type_id,
                                ref_id=object_ref.id,
                                old_values=None,
                                new_values=new_snapshot,
                                actor_id=hook_actor_id,
                                origin=import_mutation_origin.as_payload(),
                            )
                else:
                    # Regular value (single or multi)
                    await _write_object_values(
                        db, object_id, attr_id, entry,
                        actor_id=actor_id,
                        data_type=data_type,
                        structure=(attr_ref or {}).get("structure"),
                    )

                    # Keep Object.name (the label) in sync with the "Name" attribute's
                    # value — same rule as object_value_service._sync_object_name, so
                    # a CSV-driven rename of the "Name" column actually updates the
                    # label instead of only writing the underlying ObjectValue. Single-
                    # value entries only, matching _sync_object_name's value_simple check.
                    attr_name = attr_ref["name"] if attr_ref else None
                    if (
                        attr_name == "Name"
                        and object_obj is not None
                        and not (isinstance(entry, dict) and "values" in entry)
                    ):
                        new_name = _extract_typed_value(entry, data_type)
                        if new_name and object_obj.name != str(new_name):
                            object_obj.name = str(new_name)

            # ── 5f: Emit the row's Object event (D-08/D-14) ────────────
            # Exactly one on_object_created for a new row; for a matched row one
            # on_object_updated only when the Object columns (label) or a typed
            # scalar value actually changed. A ref-only row emits no general
            # Object update (D-03). The hook shares this row's transaction and
            # fires BEFORE its commit (D-04/D-05).
            new_obj_snapshot = model_to_dict(object_obj)
            changed_attribute_ids: list[uuid.UUID] = []
            attribute_changes: list[dict] = []
            for attr_id_str, entry in row_data.items():
                attr_id = attr_id_str if isinstance(attr_id_str, uuid.UUID) else uuid.UUID(attr_id_str)
                if entry is None or attr_id in skip_attrs:
                    continue
                if isinstance(entry, dict) and "ref_object_ids" in entry:
                    continue
                persisted_rows = (await db.execute(
                    select(ObjectValue).where(
                        ObjectValue.object_id == object_id,
                        ObjectValue.object_type_attribute_id == attr_id,
                    )
                )).scalars().all()
                changed_attribute_ids.append(attr_id)
                attribute_changes.append({
                    "attribute_id": attr_id,
                    "old": old_value_snapshots.get(attr_id, []),
                    "new": [model_to_dict(ov) for ov in persisted_rows],
                })

            if is_new_object:
                await _fire_object_hook(
                    "on_object_created",
                    db,
                    object_id=object_id,
                    schema_id=schema_id,
                    object_type_id=object_type_id,
                    new_values=new_obj_snapshot,
                    actor_id=hook_actor_id,
                    origin=import_mutation_origin.as_payload(),
                    changed_attribute_ids=changed_attribute_ids,
                    attribute_changes=attribute_changes,
                )
            elif old_obj_snapshot != new_obj_snapshot or changed_attribute_ids:
                await _fire_object_hook(
                    "on_object_updated",
                    db,
                    object_id=object_id,
                    schema_id=object_obj.schema_id,
                    object_type_id=object_obj.object_type_id,
                    old_values=old_obj_snapshot,
                    new_values=new_obj_snapshot,
                    actor_id=hook_actor_id,
                    origin=import_mutation_origin.as_payload(),
                    changed_attribute_ids=changed_attribute_ids,
                    attribute_changes=attribute_changes,
                )

            # One IndexOutbox entry per row (not per attribute) — reuses the same
            # helper object_service.create_object/update_object use, instead of
            # duplicating outbox/index logic here. Scoped to once per row (which
            # touches at most one Object) so bulk imports don't multiply ARQ
            # enqueues per attribute. Cache invalidation is NOT done per-row here —
            # see the single invalidate_entity_cache() calls after the row loop,
            # which batch it once for the whole import instead of once per row.
            # CORESEAM-11/D-10/D-10a: the row carries the import origin and the
            # persisted Task creator as actor (never the synthetic system user),
            # so the worker's index hooks name the import, not the worker.
            outbox_entry = _new_outbox_entry(
                object_id, object_type_id, "index",
                actor_id=hook_actor_id, origin=import_mutation_origin,
            )
            db.add(outbox_entry)

            # Per-row commit (threat model T-156-03-02: prevents OOM on bulk)
            await db.commit()
            await _enqueue_index_job(db, object_id, "index_object_job", outbox_id=outbox_entry.id)
            successful_rows += 1
            # Phase 247 (D-12): only successfully completed rows advance
            # processed_objects — a row-level failure (caught below) never
            # increments this. Bounded by update_task_progress's own
            # 100-object/2-second write policy, so this is safe to call
            # every successful row without amplifying writes.
            if task_id is not None:
                await task_service.update_task_progress(db, task_id, processed_objects=successful_rows)
        except Exception as row_exc:
            await db.rollback()
            error_report.append({
                "row_number": row_index + 1,
                "reason": f"Unexpected error while writing row: {row_exc}",
            })
            continue

    # Invalidate the object/search Redis cache once for the whole import instead
    # of once per row — every row already gets its own IndexOutbox entry + ARQ
    # index job above (that part must stay per-object), but the cache only needs
    # purging once the whole batch is done, not thousands of times for a
    # thousand-row CSV.
    if successful_rows > 0:
        await invalidate_entity_cache("object")
        await invalidate_entity_cache("search")

    # ── Step 6: Build error report ─────────────────────────────────────
    # Collect connector-level parse errors
    for parse_error in (parse_result.errors or []):
        error_report.append({
            "row_number": parse_error.row_number,
            "reason": parse_error.reason,
        })

    # Column-mapping mismatches (e.g. wrong delimiter, renamed/typo'd headers) never
    # produce row-level errors above — fold them in explicitly so a totally-failed
    # parse (row_count == 0) still carries a diagnostic instead of error_message=None.
    for column_skip in (parse_result.columns_skipped or []):
        skip_attr_id = column_skip.get("attr_id")
        prefix = f"Column mapping skipped for attribute {skip_attr_id}: " if skip_attr_id else ""
        error_report.append({
            "row_number": None,
            "reason": f"{prefix}{column_skip.get('reason')}",
        })

    # ── Step 7: Update Import status (D-13) ────────────────────────────
    if successful_rows > 0:
        status = "completed"
    else:
        status = "failed"

    error_message_str = _build_error_message_json(error_report)

    # Always log the full, untruncated error_report — Import.error_message has a
    # 2000-char budget (_ERROR_MESSAGE_MAX_LEN) and _build_error_message_json can
    # collapse the entire list down to a single "N more error(s) truncated"
    # sentinel when individual reasons are long, silently discarding every real
    # per-row reason with no way to recover them afterwards (they only ever
    # existed in this in-memory list). Chunked records keep every reason
    # recoverable via task logs even when the DB column couldn't hold them.
    if error_report:
        chunk_size = 25
        total_chunks = (len(error_report) + chunk_size - 1) // chunk_size
        for index in range(0, len(error_report), chunk_size):
            task_logger.warning(
                "import row errors",
                extra={
                    "import_id": str(import_id),
                    "error_count": len(error_report),
                    "chunk": f"{index // chunk_size + 1}/{total_chunks}",
                    "errors": error_report[index:index + chunk_size],
                },
            )

    import_update = ImportUpdate(
        status=status,
        total_objects=parse_result.row_count,
        processed_objects=successful_rows,
        error_message=error_message_str,
    )
    await update_import(db, import_id, import_update)

    # Phase 247 (D-12/D-13): force the exact final successful/total pair
    # onto the Task before the caller's own terminal Task status update —
    # bypasses the write-threshold so the last few rows' progress is never
    # silently dropped.
    if task_id is not None:
        await task_service.update_task_progress(
            db, task_id, processed_objects=successful_rows, total_objects=parse_result.row_count, force=True
        )

    # Temp file cleanup is handled by the execute_csv_import wrapper's finally
    # block (WR-01) — not here, so cleanup also runs on exceptions raised above.

    # ── Step 9: Return result ──────────────────────────────────────────
    return {
        "success": True,
        "import_definition_id": str(import_definition_id),
        "import_id": str(import_id),
        "row_count": parse_result.row_count,
        "processed_count": successful_rows,
        "error_count": len(error_report),
    }
