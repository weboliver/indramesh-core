"""CSV export execution orchestration.

Queries all required data from the DB (ExportDefinition, Target, Objects, ObjectValues,
ObjectRefs, attribute metadata), bundles it into plain dicts, and delegates formatting
to the pure CsvExportConnector. Keeps the ARQ worker thin and the connector pure.
"""

import os
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.export_definition import ExportDefinition
from app.models.target import Target
from app.models.object import Object
from app.models.object_value import ObjectValue
from app.models.object_ref import ObjectRef
from app.models.object_type_attribute import ObjectTypeAttribute
from app.models.attribute_type import AttributeType
from app.models.status import Status
from app.models.task import Task
from app.models.user import User
from app.services.connectors.csv_connector import CsvExportConnector, CsvExportResult
from app.services.export_service import update_export
from app.services.object_ref_service import hidden_ref_clause
from app.services.select_option_service import build_option_map
from app.services.value_visibility import (
    resolve_principal_roles,
    visible_attribute_conditions,
    visible_object_ids_subquery,
)
from app.schemas.crud import ExportUpdate
from app.services import task_service

# WR-04: Target.config["file_path"] is a client-settable string (gated only by
# require_permission("targets", "create")) with no path-sanitizing validator. Mirrors
# the EXPORT_ROOT containment check applied to the read side in app/api/exports.py (CR-01)
# so a Target can't be pointed at an arbitrary writable location on the backend host.
EXPORT_ROOT = os.path.realpath(os.getenv("EXPORT_ROOT", "/tmp/exports"))


def _sanitize_filename(name: str) -> str:
    """Replace non-alphanumeric chars with _, strip leading/trailing _.

    Consecutive non-alphanumeric characters collapse to a single underscore so
    "My Export!@# Definition" becomes "My_Export_Definition" (not
    "My_Export____Definition").
    """
    sanitized = re.sub(r"[^a-zA-Z0-9]+", "_", name)
    return sanitized.strip("_") or "export"


# EXT-03-H5 (D-19): asyncpg accepts at most 32 767 bind parameters in one
# statement — measured once by a probe against the live driver (32 767
# succeed, 32 768 raise ``InterfaceError``). The value and reference
# selections below are subqueries now and bind none of the object ids; this
# bound covers the one remaining materialised id list in the export path, the
# reference *targets*, whose ids come from already-loaded rows and therefore
# cannot become a subquery.
MAX_BIND_IDS = 5_000


async def _load_by_id_chunks(
    db: AsyncSession,
    model,
    column,
    ids,
    *,
    chunk_size: int = MAX_BIND_IDS,
    extra_conditions: tuple = (),
) -> list:
    """Load ``model`` rows whose ``column`` is in ``ids``, ``chunk_size`` binds at a time.

    One statement per chunk keeps every bind list under the driver ceiling; a
    caller that already holds the ids in memory (reference targets) has no
    query-shaped alternative. ``extra_conditions`` are additional WHERE
    clauses — the export passes the principal's visible-object predicate so a
    lookup can never load a row outside the caller's scope.
    """
    unique_ids = list(dict.fromkeys(ids))
    rows: list = []
    for start in range(0, len(unique_ids), chunk_size):
        statement = select(model).where(
            column.in_(unique_ids[start:start + chunk_size])
        )
        for condition in extra_conditions:
            statement = statement.where(condition)
        result = await db.execute(statement)
        rows.extend(result.scalars().all())
    return rows


def _object_scope_subquery(object_type_id: uuid.UUID):
    """``select(objects.id)`` for one ObjectType — the export's row universe.

    A subquery, never a materialised id list: ``IN (SELECT ...)`` binds no
    parameter per object, so an ObjectType of any size stays under the driver's
    parameter ceiling (EXT-03-H5).
    """
    return select(Object.id).where(Object.object_type_id == object_type_id)


def _value_selection_statement(object_type_id: uuid.UUID, *, object_scope=None):
    """ObjectValue selection scoped by the object subquery (no id list)."""
    scope = object_scope if object_scope is not None else _object_scope_subquery(object_type_id)
    return select(ObjectValue).where(ObjectValue.object_id.in_(scope))


def _ref_selection_statement(
    object_type_id: uuid.UUID, *, object_scope=None, target_scope=None
):
    """ObjectRef selection scoped by the object subquery on **both** ends.

    Mirrors the REST reference path's ``visible_ref_conditions``: a reference
    whose other end is record-DENIED is hidden completely, so its target's
    name never reaches the CSV. ``target_scope`` is the principal's
    visible-object subquery *without* the exported ObjectType restriction — a
    reference may legitimately target another type, and only the visibility
    predicate (not the export's row universe) applies to that end.
    """
    scope = object_scope if object_scope is not None else _object_scope_subquery(object_type_id)
    target_scope = target_scope if target_scope is not None else scope
    return select(ObjectRef).where(
        ObjectRef.from_object_id.in_(scope),
        ObjectRef.to_object_id.in_(target_scope),
    )


async def _value_selection_for_principal(
    db: AsyncSession,
    object_type_id: uuid.UUID,
    object_scope,
    principal_id: uuid.UUID,
    is_superuser: bool,
    role_ids: list[uuid.UUID] | None,
):
    """ObjectValue selection for the export: object scope + attribute predicate.

    The attribute-type record DENY and ``is_hidden`` are applied here, at
    selection time — never to the produced file, so no intermediate CSV carries
    a denied value. A superuser receives no clause (RLR-05 bypass).
    """
    statement = (
        _value_selection_statement(object_type_id, object_scope=object_scope)
        .join(
            ObjectTypeAttribute,
            ObjectTypeAttribute.id == ObjectValue.object_type_attribute_id,
        )
        .join(AttributeType, AttributeType.id == ObjectTypeAttribute.attribute_type_id)
    )
    for clause in await visible_attribute_conditions(
        db, str(principal_id), is_superuser, role_ids=role_ids
    ):
        statement = statement.where(clause)
    return statement


async def _resolve_export_principal(
    db: AsyncSession,
    task_id: uuid.UUID | None,
    principal_id: uuid.UUID | None,
) -> uuid.UUID:
    """The user whose visibility the export rows are keyed on — fail closed.

    ``Export.creator_id`` records who *owns* the row, but it is not the
    authority for content selection here: the principal comes either from the
    explicit ``principal_id`` (direct callers) or from ``Task.creator_id`` (the
    worker path; a scheduled clone inherits the permanent template's creator, a
    retry inherits the original row's).

    A task row whose ``creator_id`` is NULL is a legacy or system task; it must
    fail closed instead of inventing a principal, because exporting unfiltered
    rows for it would reopen exactly the leak the selection below closes.
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
            f"Export task {task_id} has no creator; refusing to export without "
            "a resolvable principal"
        )
    raise ValueError(
        "Export requires a resolvable principal: pass principal_id or a task_id "
        "whose creator is set"
    )


async def _resolve_export_visibility(
    db: AsyncSession, principal_id: uuid.UUID
) -> tuple[bool, list[uuid.UUID] | None]:
    """``(is_superuser, role_ids)`` for the acting principal.

    A principal with no matching user row fails closed — the same refusal as an
    unresolvable principal, never an unfiltered export.
    """
    user = (
        await db.execute(select(User).where(User.id == principal_id))
    ).scalar_one_or_none()
    if user is None:
        raise ValueError(f"Export principal {principal_id} does not exist")
    role_ids = await resolve_principal_roles(db, str(user.id), user.is_superuser)
    return user.is_superuser, role_ids


async def execute_csv_export(
    db: AsyncSession,
    export_definition_id: uuid.UUID,
    export_id: uuid.UUID,
    task_id: uuid.UUID | None = None,
    principal_id: uuid.UUID | None = None,
) -> CsvExportResult:
    """Orchestrate a CSV export execution.

    1. Resolve the acting principal (fail closed when none resolves)
    2. Load ExportDefinition + Target config
    3. Query Objects visible to that principal for the definition's object_type_id
    4. Query ObjectValues and ObjectRefs for those objects (attribute-filtered)
    5. Preload attribute metadata and lookup caches
    6. Delegate to CsvExportConnector for formatting
    7. Write CSV file to Target's configured file_path directory
    8. Update Export.object_key with resolved file path

    `task_id` (Phase 247, D-12/D-13) is optional: when supplied, the real
    selected-Object total and the completed row count are published onto
    the Task via `task_service.update_task_progress` — the caller's own
    Export counters are unaffected either way.

    `principal_id` (317-08, SEC313-17) is the user the row selection is keyed
    on. Without it the principal is `Task.creator_id` for the supplied
    `task_id`; when neither resolves, the call raises before any selection
    query or file write. See `_resolve_export_principal`.
    """
    # D-05/SEC313-17: resolve the acting principal before anything else so an
    # unattributable export writes no file and queries no row.
    principal_id = await _resolve_export_principal(db, task_id, principal_id)
    is_superuser, role_ids = await _resolve_export_visibility(db, principal_id)

    # Load ExportDefinition
    result = await db.execute(
        select(ExportDefinition).where(ExportDefinition.id == export_definition_id)
    )
    definition = result.scalar_one_or_none()
    if not definition:
        raise ValueError(f"ExportDefinition {export_definition_id} not found")

    # Load Target for CSV config (file_path, delimiter, encoding)
    result = await db.execute(
        select(Target).where(Target.id == definition.target_id)
    )
    target = result.scalar_one_or_none()
    if not target:
        raise ValueError(f"Target {definition.target_id} not found for ExportDefinition")

    target_config = target.config or {}
    if not target.export_folder:
        raise ValueError(f"Target {target.id} has no export_folder configured")
    file_dir = os.path.join(EXPORT_ROOT, target.export_folder)
    delimiter = target_config.get("delimiter", ",")
    encoding = target_config.get("encoding", "utf-8")

    # WR-04: reject any export_folder that resolves outside EXPORT_ROOT before ever
    # touching the filesystem (directory traversal, absolute paths, symlink escapes).
    resolved_dir = os.path.realpath(file_dir)
    if resolved_dir != EXPORT_ROOT and not resolved_dir.startswith(EXPORT_ROOT + os.sep):
        raise ValueError(
            f"Target export_folder '{target.export_folder}' resolves outside the "
            f"permitted export root ({EXPORT_ROOT})"
        )
    file_dir = resolved_dir

    # Ensure output directory exists
    os.makedirs(file_dir, exist_ok=True)

    # Resolve filename
    timestamp = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y%m%d_%H%M%S")
    safe_name = _sanitize_filename(definition.name)
    filename = f"{safe_name}_{timestamp}.csv"
    file_path = os.path.join(file_dir, filename)

    # D-05/SEC313-17: the export's row set is the acting principal's, not the
    # worker's. The object-level predicate is applied at selection time, so a
    # denied object's row is never loaded and no intermediate state carries it.
    # `visible_objects` stays unrestricted by ObjectType: it is the both-ends
    # reference predicate's source, and reference targets may live in another
    # type.
    visible_objects = await visible_object_ids_subquery(
        db, str(principal_id), is_superuser, role_ids=role_ids
    )
    object_scope = visible_objects.where(
        Object.object_type_id == definition.object_type_id
    )

    result = await db.execute(select(Object).where(Object.id.in_(object_scope)))
    objects = result.scalars().all()

    # Phase 247 (D-12): the real selected-Object count is the honest CSV
    # export total — publish it as soon as it is known, before formatting.
    if task_id is not None:
        await task_service.update_task_progress(db, task_id, total_objects=len(objects))

    # Load ObjectValues for all objects. EXT-03-H5: the scope is the object
    # subquery, never an id list — an IN (SELECT ...) binds no parameter per
    # object, so an ObjectType of any size stays below the driver ceiling.
    result = await db.execute(
        await _value_selection_for_principal(
            db, definition.object_type_id, object_scope, principal_id,
            is_superuser, role_ids,
        )
    )
    all_values = result.scalars().all()

    # Group values by object_id
    values_by_object: dict[uuid.UUID, list[ObjectValue]] = {}
    for v in all_values:
        values_by_object.setdefault(v.object_id, []).append(v)

    # Load ObjectRefs for all objects (same subquery scope as the values).
    # The hidden reference-attribute decision is metadata, not principal-scoped
    # (SEC313-97), so it applies to every caller.
    ref_statement = _ref_selection_statement(
        definition.object_type_id,
        object_scope=object_scope,
        target_scope=visible_objects,
    )
    hidden_reference_clause = await hidden_ref_clause(db)
    if hidden_reference_clause is not None:
        ref_statement = ref_statement.where(hidden_reference_clause)
    result = await db.execute(ref_statement)
    all_refs = result.scalars().all()

    # Group refs by from_object_id
    refs_by_object: dict[uuid.UUID, list[ObjectRef]] = {}
    for r in all_refs:
        refs_by_object.setdefault(r.from_object_id, []).append(r)

    # Preload attribute cache + type_cache
    # type_cache is pre-extracted in async context to avoid MissingGreenletError
    # when the synchronous CsvExportConnector accesses data_type later
    attr_ids = set()
    for v in all_values:
        attr_ids.add(v.object_type_attribute_id)
    for mapping in definition.field_mappings:
        if isinstance(mapping["attr_id"], str):
            attr_ids.add(uuid.UUID(mapping["attr_id"]))
        else:
            attr_ids.add(mapping["attr_id"])

    attr_cache: dict[uuid.UUID, ObjectTypeAttribute] = {}
    type_cache: dict[uuid.UUID, str] = {}  # attr_id -> data_type string
    if attr_ids:
        result = await db.execute(
            select(ObjectTypeAttribute)
            .options(selectinload(ObjectTypeAttribute.attribute_type))
            .where(ObjectTypeAttribute.id.in_(list(attr_ids)))
        )
        for attr in result.scalars().all():
            attr_cache[attr.id] = attr
            if attr.attribute_type:
                # data_type is a plain String(50) column (see AttributeType model), not a native
                # SQLAlchemy Enum — SQLAlchemy returns a plain str here, never an enum instance,
                # so .value raised AttributeError on every row.
                type_cache[attr.id] = attr.attribute_type.data_type

    # Preload status name cache
    status_cache: dict[uuid.UUID, str] = {}
    for v in all_values:
        data_type = type_cache.get(v.object_type_attribute_id)
        if data_type == "status" and v.value_simple:
            try:
                status_uuid = uuid.UUID(v.value_simple)
                if status_uuid not in status_cache:
                    s_result = await db.execute(
                        select(Status).where(Status.id == status_uuid)
                    )
                    status_obj = s_result.scalar_one_or_none()
                    if status_obj:
                        status_cache[status_uuid] = status_obj.name
            except (ValueError, AttributeError):
                pass

    # Preload user/group name caches (D-15: str(uuid) -> username/name), only
    # when the exported ObjectType's attributes actually include USER/GROUP
    # types — mirrors status_cache's shape (id -> display value).
    user_cache: dict[str, str] = {}
    group_cache: dict[str, str] = {}
    if any(dt == "user" for dt in type_cache.values()):
        from app.models.user import User
        users_result = await db.execute(select(User))
        for u in users_result.scalars().all():
            user_cache[str(u.id)] = u.username
    if any(dt == "group" for dt in type_cache.values()):
        from app.models.group import Group
        groups_result = await db.execute(select(Group))
        for g in groups_result.scalars().all():
            group_cache[str(g.id)] = g.name

    # Preload SELECT option cache (D-12: id -> base display value, built from
    # the already-loaded attr_cache structures; base-language only — no lang
    # dimension, translations never consulted).
    select_cache: dict[uuid.UUID, dict[str, str]] = {}
    for attr_id, attr in attr_cache.items():
        if type_cache.get(attr_id) == "select":
            option_map = build_option_map(attr)
            if option_map:
                select_cache[attr_id] = option_map

    # Preload object name cache (for reference display)
    obj_name_cache: dict[uuid.UUID, str] = {}
    ref_target_ids = set()
    for ref_list in refs_by_object.values():
        for ref in ref_list:
            ref_target_ids.add(ref.to_object_id)
    if ref_target_ids:
        # EXT-03-H5: the target ids come from already-loaded ref rows, so this
        # list cannot become a subquery — bound it per statement instead. The
        # visible-object predicate is applied here as well: the ref selection
        # already dropped hidden targets, and this keeps the name cache itself
        # from ever carrying a denied name.
        for obj in await _load_by_id_chunks(
            db, Object, Object.id, ref_target_ids,
            extra_conditions=(Object.id.in_(visible_objects),),
        ):
            obj_name_cache[obj.id] = obj.name

    # Build CSV
    connector = CsvExportConnector(delimiter=delimiter, encoding=encoding)
    csv_result = connector.build_csv(
        objects=objects,
        values_by_object=values_by_object,
        refs_by_object=refs_by_object,
        field_mappings=definition.field_mappings,
        attr_cache=attr_cache,
        type_cache=type_cache,
        status_cache=status_cache,
        select_cache=select_cache,
        obj_name_cache=obj_name_cache,
        user_cache=user_cache,
        group_cache=group_cache,
    )

    # CR-03: build_csv returns row_count=0/columns=[] whenever every field mapping
    # failed to resolve (empty field_mappings, or every attr_id stale/deleted — see
    # the `skipped` branch in CsvExportConnector.build_csv). Previously file_path was
    # set unconditionally even when no file was written, so the caller in worker.py
    # would mark the task SUCCESS / export COMPLETED pointing at a path that was
    # never created. Raise instead so worker.py's except-clause marks it FAILED with
    # a useful error_message.
    if not csv_result.columns:
        raise ValueError(
            f"No valid field mappings resolved for export definition "
            f"{export_definition_id} — {len(csv_result.columns_skipped)} skipped"
        )

    with open(file_path, "w", newline="", encoding=encoding) as f:
        f.write(csv_result.csv_content)

    csv_result.file_path = file_path

    # Update Export.object_key with resolved path. System-authored: this is the
    # CSV executor's own write, never a caller-supplied value (no request schema
    # carries object_key).
    await update_export(db, export_id, ExportUpdate(), object_key=file_path)

    # Phase 247 (D-12/D-13): force the exact final row-count pair onto the
    # Task before the caller's own terminal Task status update.
    if task_id is not None:
        await task_service.update_task_progress(
            db, task_id, processed_objects=csv_result.row_count, total_objects=csv_result.row_count, force=True
        )

    return csv_result
