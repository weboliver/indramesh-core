import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import List
from zoneinfo import available_timezones

from apscheduler.triggers.cron import CronTrigger
from cron_descriptor import get_description
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schedule import Schedule
from app.models.task import Task, TaskType
from app.models.export_definition import ExportDefinition
from app.schemas.crud import (
    ScheduleCreate,
    ScheduleResponse,
    ScheduleUpdate,
    SchedulePreviewRequest,
    SchedulePreviewResponse,
)
from app.services import source_service, schema_service, target_service, object_type_service
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
# Phase 289 (289-04 review correction 5): the Core schedulability/scope rules
# live in ONE descriptor table (task_type_catalog) that the task-type catalog,
# this service and Plan 289-07's plugin-aware validation all read. The names
# stay importable from here for the existing callers/tests.
from app.services.task_type_catalog import (
    CORE_TASK_TYPE_DESCRIPTORS,
    CORE_TASK_TYPES as VALID_TASKTYPES,
)
from app.services.scheduler_manager import scheduler_manager

ENTITY_TYPE = "schedule"


class SchedulePermissionError(ValueError):
    """The authenticated caller lacks a declaration's ``required_permission``.

    Phase 289 (CORESEAM-07, D-06/D-04). A ``ValueError`` subclass so every
    pre-289 handler keeps working, while the Schedule routes map it to 403
    before the generic 422 handler (the 289-04 typed-admission precedent).
    """


@dataclass(frozen=True)
class ScheduleTaskTypeResolution:
    """A schedule tasktype resolved to its Core descriptor or plugin declaration.

    ``kind`` is ``"core"`` for a descriptor from the one Core table and
    ``"plugin"`` for a declaration from this process's task-type snapshot. The
    scope requirements and the declared permission come from the same source
    the catalog and central admission read — schedule validation never keeps a
    second copy of either.
    """

    kind: str
    schedulable: bool
    scope_requirements: tuple[str, ...]
    required_permission: str | None = None
    plugin_name: str | None = None


def resolve_schedule_task_type(
    literal: str | None,
) -> ScheduleTaskTypeResolution | None:
    """Resolve a schedule's tasktype against this process's ONE registry (D-01).

    Core types resolve from ``task_type_catalog.CORE_TASK_TYPE_DESCRIPTORS``;
    a qualified literal resolves against the process-local plugin snapshot
    (``get_process_task_type_registry``), so the backend API and the worker's
    APScheduler refresh each honour the declarations their OWN process loaded
    (Plan 289-04 review correction 6). ``None`` means neither — unschedulable.
    """
    for descriptor in CORE_TASK_TYPE_DESCRIPTORS:
        if descriptor.name == literal:
            return ScheduleTaskTypeResolution(
                kind="core",
                schedulable=descriptor.schedulable,
                scope_requirements=tuple(descriptor.scope_requirements),
            )
    if not literal:
        return None

    # Function-local: schedule_service must not import app.plugin_system at
    # module import time (the plugin bootstrap path imports services).
    from app.plugin_system.task_registry import get_process_task_type_registry

    entry = get_process_task_type_registry().resolve(literal)
    if entry is None:
        return None
    declaration = entry.declaration
    return ScheduleTaskTypeResolution(
        kind="plugin",
        schedulable=bool(declaration.schedulable),
        scope_requirements=tuple(declaration.scope_requirements or ()),
        required_permission=declaration.required_permission,
        plugin_name=entry.plugin_name,
    )


async def _check_declared_permission(
    resolution: ScheduleTaskTypeResolution,
    current_user,
    tasktype: str,
) -> None:
    """Enforce a plugin declaration's ``required_permission`` (D-06).

    Checked exactly once, at the authenticated create/update/run-now boundary:
    the resolved effective permission set (deny-wins, server-side) must hold
    the declaration's permission unless the caller is a superuser (PERM-05
    parity). No owner is persisted and automated runs never recheck a user's
    role. Core types and declarations without a required_permission need no
    extra permission beyond the route's existing schedules gate (D-04/D-05).

    Runs BEFORE the schedulability/scope checks so a 403 never discloses a
    declaration's runtime detail (T-289-07-01).
    """
    required_permission = resolution.required_permission
    if resolution.kind != "plugin" or required_permission is None:
        return
    if current_user is None:
        raise SchedulePermissionError(
            f"tasktype {tasktype!r} requires an authenticated user with "
            f"permission: {required_permission}."
        )
    if getattr(current_user, "is_superuser", False):
        return

    from app.services.auth_service import resolve_user_permissions

    permissions = await resolve_user_permissions(str(current_user.id))
    if required_permission not in permissions:
        raise SchedulePermissionError(
            f"Not authorized to schedule task type {tasktype!r}: missing "
            f"permission: {required_permission}."
        )


async def authorize_schedule_action(schedule: Schedule, current_user) -> None:
    """Authorize a manual run-now against the PERSISTED schedule's declaration.

    D-04/D-06 (Plan 289-07): a manual HTTP trigger is a user action and is
    reauthorized for the CURRENT caller; the unattended cron callback never
    consults a user role. Core schedules and declarations without a
    required_permission are unchanged.
    """
    resolution = resolve_schedule_task_type(schedule.tasktype)
    if resolution is None:
        return
    await _check_declared_permission(resolution, current_user, schedule.tasktype)


def can_access_schedule(
    schedule: Schedule | dict, user_id, is_superuser: bool
) -> bool:
    """Ownership gate: superuser bypass, otherwise creator equality — fail closed.

    A row with ``creator_id`` NULL (pre-261022 rows, a deleted creator, a
    system-created schedule) is superuser-only. Cached payloads are plain dicts
    whose ``creator_id`` would be a JSON string, hence the string comparison
    against either shape.
    """
    if is_superuser:
        return True
    if isinstance(schedule, dict):
        creator_id = schedule.get("creator_id")
    else:
        creator_id = schedule.creator_id
    if creator_id is None:
        return False
    return str(creator_id) == str(user_id)



# --- Phase 289 (CORESEAM-07, D-01/D-02/D-03): runtime-only projection ---------

# The three response-only runtime states. `disabled` is USER intent
# (Schedule.enabled=false) and stays distinguishable from a runtime `paused`.
RUNTIME_STATUS_SCHEDULED = "scheduled"
RUNTIME_STATUS_PAUSED = "paused"
RUNTIME_STATUS_DISABLED = "disabled"


def _apply_runtime(schedule: Schedule, status: str, reason: str | None) -> None:
    """Attach the response-only runtime projection to a Schedule row.

    Plain (non-mapped) instance attributes on purpose: they are a projection of
    persisted truth, never columns. The Pydantic response model reads them via
    ``from_attributes`` and ``ScheduleResponse.model_validate(...).model_dump()``
    keeps them alive through the list/entity caches (``jsonable_encoder`` on the
    ORM rows would silently drop them).
    """
    schedule.runtime_status = status
    schedule.runtime_reason = reason


async def schedule_runtime_states(
    db: AsyncSession, schedules
) -> dict[uuid.UUID, tuple[str, str | None]]:
    """Project each schedule's ``(runtime_status, runtime_reason)`` (D-01/D-02).

    Derived from PERSISTED truth only — ``Schedule.enabled``, the resolved
    declaration in THIS process's registry, and the ``plugins`` row (``state``,
    ``version``, ``worker_loaded_version``) — never from APScheduler job
    presence. The dispatchability predicate is the same one the task-type
    catalog reports as ``dispatchable`` (Plan 289-04, D-10):

    - ``enabled=false`` → ``disabled`` (the user's own decision; no reason).
    - an enabled Core/legacy schedule → ``scheduled`` (pre-289 behaviour).
    - an enabled plugin schedule → ``scheduled`` only when its declaration
      resolves here, the plugin row is ACTIVE and the worker loaded the active
      version; otherwise ``paused`` with a bounded, secret-free reason naming
      plugin/state (or both versions, via the shared wording helper).

    The scheduler only ever removes/omits the APScheduler job for a paused
    schedule; ``Schedule.enabled`` is never rewritten (T-289-07-04: the reason
    composer emits plugin name/state/versions only — no config, params, or
    traceback).
    """
    from app.models.plugin import Plugin, PluginState
    from app.services.task_type_catalog import plugin_version_conflict_reason

    states: dict[uuid.UUID, tuple[str, str | None]] = {}
    plugin_schedules: dict[uuid.UUID, tuple[str, ScheduleTaskTypeResolution]] = {}
    needed_plugins: set[str] = set()

    for schedule in schedules:
        if not schedule.enabled:
            states[schedule.id] = (RUNTIME_STATUS_DISABLED, None)
            continue
        resolution = resolve_schedule_task_type(schedule.tasktype)
        if resolution is None:
            # An enabled literal that no longer resolves here (plugin removed
            # from disk, declaration withdrawn) cannot dispatch — runtime-pause
            # it instead of advertising a run that can never start.
            states[schedule.id] = (
                RUNTIME_STATUS_PAUSED,
                f"task type {schedule.tasktype!r} is not a declared plugin task type",
            )
            continue
        if resolution.kind == "core":
            states[schedule.id] = (RUNTIME_STATUS_SCHEDULED, None)
            continue
        plugin_schedules[schedule.id] = (schedule.tasktype, resolution)
        if resolution.plugin_name:
            needed_plugins.add(resolution.plugin_name)

    if plugin_schedules:
        rows_by_name: dict[str, object] = {}
        if needed_plugins:
            result = await db.execute(
                select(Plugin).where(Plugin.name.in_(sorted(needed_plugins)))
            )
            rows_by_name = {row.name: row for row in result.scalars().all()}

        for schedule_id, (literal, resolution) in plugin_schedules.items():
            row = rows_by_name.get(resolution.plugin_name)
            if row is None:
                states[schedule_id] = (
                    RUNTIME_STATUS_PAUSED,
                    f"plugin {resolution.plugin_name!r} is not installed",
                )
                continue
            state_value = (
                row.state.value if isinstance(row.state, PluginState) else row.state
            )
            if state_value != PluginState.ACTIVE.value:
                states[schedule_id] = (
                    RUNTIME_STATUS_PAUSED,
                    f"plugin {resolution.plugin_name!r} is not available "
                    f"(state={state_value!r})",
                )
                continue
            if row.worker_loaded_version != row.version:
                states[schedule_id] = (
                    RUNTIME_STATUS_PAUSED,
                    plugin_version_conflict_reason(
                        resolution.plugin_name,
                        row.worker_loaded_version,
                        row.version,
                    ),
                )
                continue
            states[schedule_id] = (RUNTIME_STATUS_SCHEDULED, None)

    return states


async def project_schedule_runtime(db: AsyncSession, schedules) -> None:
    """Set ``runtime_status``/``runtime_reason`` on each Schedule row (D-02)."""
    if not schedules:
        return
    states = await schedule_runtime_states(db, schedules)
    for schedule in schedules:
        status, reason = states.get(
            schedule.id, (RUNTIME_STATUS_SCHEDULED, None)
        )
        _apply_runtime(schedule, status, reason)


async def _runtime_for(
    db: AsyncSession, schedule: Schedule
) -> tuple[str, str | None]:
    """The runtime projection pair for ONE schedule (create/update responses)."""
    states = await schedule_runtime_states(db, [schedule])
    return states.get(schedule.id, (RUNTIME_STATUS_SCHEDULED, None))


async def _resolve_template(
    db: AsyncSession,
    data: ScheduleCreate | ScheduleUpdate,
    *,
    current_user=None,
) -> None:
    """Mutates `data` in place: validates template_id/template_type, auto-derives tasktype,
    and (for template_type='task') auto-fills the nullable scope columns from the template
    Task. No-op if neither template field is set (legacy scope-based schedule).

    A ``task`` template must belong to the caller (superuser exempt): scheduling
    someone else's permanent template would let a caller run that user's task
    forever. A mismatch reads exactly like a missing template, so a foreign id
    never confirms its existence. IMPORT/EXPORT task templates are refused for
    every caller (superuser included): their clone carries no
    import/export definition and could never run, and an EXPORT definition is
    scheduled through ``template_type='export'`` instead.
    """
    template_type = data.template_type
    template_id = data.template_id
    if not template_type and not template_id:
        return
    if bool(template_type) != bool(template_id):
        raise ValueError("template_id and template_type must be provided together")

    if template_type == "task":
        template_query = select(Task).where(
            Task.id == template_id, Task.schedule_id.is_(None)
        )
        if current_user is not None and not getattr(current_user, "is_superuser", False):
            template_query = template_query.where(
                Task.creator_id == current_user.id
            )
        result = await db.execute(template_query)
        template_task = result.scalar_one_or_none()
        if not template_task:
            raise ValueError(f"Task template not found: {template_id}")
        if template_task.tasktype == TaskType.EXPORT.value:
            raise ValueError(
                f"Template task {template_id} is an EXPORT task — schedule its "
                "ExportDefinition with template_type='export' instead"
            )
        if template_task.tasktype == TaskType.IMPORT.value:
            raise ValueError(
                f"Template task {template_id} is an IMPORT task — CSV imports "
                "cannot be scheduled; use POST /imports/csv/upload instead"
            )
        if not data.tasktype:
            data.tasktype = template_task.tasktype
        # D-03: scope columns are denormalized filter helpers — fill from the template
        # only where the caller didn't already set them explicitly.
        if data.source_id is None:
            data.source_id = template_task.source_id
        if data.schema_id is None:
            data.schema_id = template_task.schema_id
        if data.object_type_id is None:
            data.object_type_id = template_task.object_type_id
        if data.target_id is None:
            data.target_id = template_task.target_id

    elif template_type == "export":
        result = await db.execute(
            select(ExportDefinition.id).where(ExportDefinition.id == template_id)
        )
        if not result.scalar_one_or_none():
            raise ValueError(f"ExportDefinition not found: {template_id}")
        if not data.tasktype:
            data.tasktype = TaskType.EXPORT.value

    elif template_type == "import":
        raise ValueError(
            "Scheduling an ImportDefinition is not supported — CSV imports require a "
            "persistent data source that isn't available yet. Use POST /imports/csv/upload "
            "for one-off imports instead."
        )

    else:
        raise ValueError(f"Unknown template_type: {template_type}. Must be 'task' or 'export'")


def _compute_next_run(cron_expression: str, timezone: str) -> datetime | None:
    """Compute the next fire time for a cron expression (stored as UTC)."""
    try:
        from zoneinfo import ZoneInfo
        trigger = CronTrigger.from_crontab(cron_expression, timezone=timezone or "UTC")
        next_fire = trigger.get_next_fire_time(None, datetime.now(ZoneInfo(timezone or "UTC")))
        if next_fire is None:
            return None
        # Convert to UTC, then strip tz — DB column is DateTime (no tz)
        return next_fire.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    except Exception:
        return None


def _compile_preset(preset_type: str, params: dict) -> str:
    """Compile preset parameters to a cron expression (D-12)."""
    if preset_type == "EVERY_N_MINUTES":
        minutes = params.get("interval_minutes")
        if not minutes or minutes < 1 or minutes > 59:
            raise ValueError("interval_minutes must be 1-59")
        return f"*/{minutes} * * * *"
    elif preset_type == "EVERY_N_HOURS":
        hours = params.get("interval_hours")
        if not hours or hours < 1 or hours > 23:
            raise ValueError("interval_hours must be 1-23")
        return f"0 */{hours} * * *"
    elif preset_type == "DAILY":
        hour = params.get("hour", 0)
        minute = params.get("minute", 0)
        if not (0 <= hour <= 23) or not (0 <= minute <= 59):
            raise ValueError("hour must be 0-23, minute must be 0-59")
        return f"{minute} {hour} * * *"
    elif preset_type == "WEEKLY":
        day = params.get("day_of_week", 0)
        hour = params.get("hour", 0)
        minute = params.get("minute", 0)
        if not (0 <= day <= 6) or not (0 <= hour <= 23) or not (0 <= minute <= 59):
            raise ValueError("day_of_week must be 0-6, hour 0-23, minute 0-59")
        return f"{minute} {hour} * * {day}"
    else:
        raise ValueError(f"Unknown preset_type: {preset_type}")


async def _validate_schedule_data(
    data: ScheduleCreate | ScheduleUpdate,
    db: AsyncSession,
    existing: Schedule | None = None,
    *,
    current_user=None,
):
    """Validate schedule data. `existing` is the current DB row on update (None on
    create) — scope/tasktype checks run against existing-merged-with-patch so a PATCH
    that only touches one field doesn't spuriously trip validation for fields it isn't
    changing (152-REVIEW.md M1).

    Phase 289 (CORESEAM-07): the tasktype resolves through the shared Core/plugin
    descriptor resolver instead of closed local sets, so a DECLARED schedulable
    plugin task type validates with its own declaration's scope requirements. A
    plugin declaration's ``required_permission`` is checked for the authenticated
    creating/updating user here (D-06) — the one place a user permission is
    consulted; automated runs never recheck it."""

    def effective(field: str):
        val = getattr(data, field, None)
        if val is not None:
            return val
        return getattr(existing, field, None) if existing is not None else None

    tasktype = effective("tasktype")

    # CR-01 (289-REVIEW.md, T-289-07-01): the scheduled clone executes the
    # referenced template Task's `tasktype` (scheduler_manager
    # `_execute_scheduled_job` clones `template_task.tasktype` through the
    # trusted scheduler-origin admission context, which by design skips the
    # user-permission gate). The gates below must therefore authorize the
    # EFFECTIVE pair, not the declared literal alone: a caller could otherwise
    # declare a benign Core type while pointing at a permission-gated plugin
    # template. The merged (create/update) pair is checked here; `_resolve_template`
    # derives the template's value into `data.tasktype` only when the caller
    # omitted it, so an explicit conflict is rejected — never silently coerced.
    if effective("template_type") == "task" and effective("template_id") is not None:
        template_tasktype = (
            await db.execute(
                select(Task.tasktype).where(
                    Task.id == effective("template_id"), Task.schedule_id.is_(None)
                )
            )
        ).scalar_one_or_none()
        if template_tasktype is not None and template_tasktype != tasktype:
            raise ValueError(
                f"tasktype '{tasktype}' does not match the template task's tasktype "
                f"'{template_tasktype}' — a scheduled run always executes the template's type"
            )

    resolution = resolve_schedule_task_type(tasktype)
    if resolution is None:
        raise ValueError(
            f"Invalid tasktype: {tasktype}. Must be one of: {sorted(VALID_TASKTYPES)} "
            "or a declared plugin task type"
        )

    # D-06/T-289-07-01: the declaration's required_permission is checked before
    # any schedulability/scope detail is disclosed.
    await _check_declared_permission(resolution, current_user, tasktype)

    if not resolution.schedulable:
        if resolution.kind == "core":
            raise ValueError(
                f"tasktype '{tasktype}' cannot be scheduled — it requires an empty destination "
                "that a repeat run would no longer satisfy"
            )
        raise ValueError(
            f"tasktype '{tasktype}' cannot be scheduled — the plugin declaration "
            "is not schedulable"
        )

    # For template_type='export', the scope columns come from the ExportDefinition's
    # ObjectType, not from the Schedule row — skip the scope check in that case.
    if effective("template_type") != "export":
        missing = [f for f in resolution.scope_requirements if effective(f) is None]
        if missing:
            raise ValueError(f"tasktype '{tasktype}' requires {', '.join(missing)}")

    object_type_id = effective("object_type_id")
    # Phase 296 (D-03): only the native IMPORT type carries a legacy
    # object_type_id narrowing allowance; every other type narrows solely
    # through its descriptor/declaration scope requirements.
    if (
        object_type_id is not None
        and tasktype != "IMPORT"
        and "object_type_id" not in resolution.scope_requirements
    ):
        raise ValueError(f"object_type_id narrowing not supported for tasktype '{tasktype}'")

    # B5 (152-REVIEW.md): cron_expression is Optional on the schema now (dual-mode with
    # preset_type per D-11) — Pydantic no longer rejects a preset-only payload before it
    # gets here, so this is the ONLY place the "at least one of the two" rule is enforced.
    # Checked against the merged (existing+patch) state so a PATCH that doesn't touch
    # either field isn't wrongly rejected. Without this check, a schedule with both fields
    # unset would pass validation and hit the DB's NOT NULL constraint on cron_expression
    # as a 500, not a 422.
    if effective("preset_type") is None and effective("cron_expression") is None:
        raise ValueError("Either preset_type or cron_expression must be provided")

    # Entity existence — only check FKs actually supplied in THIS call. An unchanged FK
    # inherited from `existing` was already validated when it was first set.
    if data.source_id is not None:
        if not await source_service.get_source(db, data.source_id):
            raise ValueError(f"Source not found: {data.source_id}")
    if data.schema_id is not None:
        if not await schema_service.get_schema(db, data.schema_id):
            raise ValueError(f"Schema not found: {data.schema_id}")
    if data.target_id is not None:
        if not await target_service.get_target(db, data.target_id):
            raise ValueError(f"Target not found: {data.target_id}")
    if data.object_type_id is not None:
        if not await object_type_service.get_object_type(db, data.object_type_id):
            raise ValueError(f"ObjectType not found: {data.object_type_id}")

    if data.timezone is not None and data.timezone not in available_timezones():
        raise ValueError(f"Invalid timezone: {data.timezone}")

    if data.preset_type is not None:
        cron = _compile_preset(data.preset_type, data.args or {})
        try:
            CronTrigger.from_crontab(cron)
        except ValueError as e:
            raise ValueError(f"Invalid preset parameters: {e}")
        data.cron_expression = cron
    elif data.cron_expression is not None:
        try:
            CronTrigger.from_crontab(data.cron_expression)
        except ValueError as e:
            raise ValueError(f"Invalid cron expression: {e}")


async def create_schedule(
    db: AsyncSession,
    schedule_data: ScheduleCreate,
    current_user=None,
) -> Schedule:
    await _resolve_template(db, schedule_data, current_user=current_user)
    await _validate_schedule_data(schedule_data, db, current_user=current_user)

    # Use global default timezone if none specified
    tz = schedule_data.timezone
    if not tz:
        from app.services.settings_service import get_setting
        tz = await get_setting(db, "default_timezone") or "UTC"

    db_schedule = Schedule(
        name=schedule_data.name,
        job_function=schedule_data.job_function,
        cron_expression=schedule_data.cron_expression,
        args=schedule_data.args or {},
        enabled=schedule_data.enabled,
        source_id=schedule_data.source_id,
        schema_id=schedule_data.schema_id,
        object_type_id=schedule_data.object_type_id,
        target_id=schedule_data.target_id,
        timezone=tz,
        tasktype=schedule_data.tasktype,
        preset_type=schedule_data.preset_type,
        template_id=schedule_data.template_id,
        template_type=schedule_data.template_type,
        # 261022-np1: ownership is server-authored — the acting user owns the
        # schedule. A system caller (current_user None) leaves it NULL, which is
        # superuser-only.
        creator_id=(
            uuid.UUID(str(current_user.id)) if current_user is not None else None
        ),
    )

    # Compute next_run_at so the API response includes it even if the worker
    # scheduler hasn't picked up the job yet.
    db_schedule.next_run_at = _compute_next_run(
        db_schedule.cron_expression, db_schedule.timezone
    )

    db.add(db_schedule)
    await db.commit()
    await db.refresh(db_schedule)
    await invalidate_entity_cache(ENTITY_TYPE)

    # Phase 289 (D-01/D-02): project the runtime-only status BEFORE the
    # registration sync, so an unavailable plugin (or stale worker) never
    # registers a job and the create response carries the projection.
    runtime_status, runtime_reason = await _runtime_for(db, db_schedule)

    # D-14: Sync to APScheduler (no-op if scheduler not running in this process)
    await scheduler_manager.add_job(db_schedule)

    # Refresh to pick up next_run_at written by scheduler_manager (if it ran)
    await db.refresh(db_schedule)
    # Re-apply after the refresh — the projection is not a mapped column.
    _apply_runtime(db_schedule, runtime_status, runtime_reason)

    return db_schedule


async def list_schedules(
    db: AsyncSession,
    skip: int = 0,
    limit: int = 100,
    *,
    user_id=None,
    is_superuser: bool = False,
) -> tuple[List[Schedule], int]:
    """List schedules the caller may see, count included.

    261022-np1: a superuser sees every row; every other caller sees only the
    rows they created. The list cache key carries the caller's scope token so a
    cached page can never be replayed across users (the export-list precedent).
    No ``user_id`` at all is a system caller: nothing is theirs — fail closed.
    """
    if user_id is None:
        return [], 0

    scope = "superuser" if is_superuser else str(user_id)
    cache_key = build_list_cache_key(
        ENTITY_TYPE, skip=skip, limit=limit, scope=scope
    )
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached["items"], cached["total"]

    count_query = select(func.count()).select_from(Schedule)
    query = select(Schedule)
    if not is_superuser:
        count_query = count_query.where(Schedule.creator_id == user_id)
        query = query.where(Schedule.creator_id == user_id)

    count_result = await db.execute(count_query)
    total = count_result.scalar()
    result = await db.execute(query.offset(skip).limit(limit))
    items = result.scalars().all()

    # Phase 289 (D-02): project the runtime-only status, then serialize through
    # the RESPONSE model — jsonable_encoder(ORM row) drops the non-mapped
    # projection (the icon_local_name precedent documented in AGENTS.md).
    await project_schedule_runtime(db, items)

    await set_cached_with_tags(
        cache_key,
        {
            "items": [
                ScheduleResponse.model_validate(item).model_dump() for item in items
            ],
            "total": total,
        },
        tags=[build_tag_key(ENTITY_TYPE)],
        ttl=CACHE_TTL_LIST,
    )
    return items, total


async def get_schedule(
    db: AsyncSession,
    schedule_id: uuid.UUID,
    *,
    user_id=None,
    is_superuser: bool = False,
) -> Schedule | dict | None:
    cache_key = build_cache_key(ENTITY_TYPE, str(schedule_id))
    # 261022-np1: the cached payload is a ScheduleResponse dump and does NOT
    # carry creator_id, so it cannot be ownership-checked. Only the allow-all
    # superuser path reads/populates the entity cache; a non-superuser always
    # reads the ORM row and is gated on it. The response shape is unchanged.
    if is_superuser:
        cached = await get_cached(cache_key)
        if cached is not None:
            return cached

    result = await db.execute(
        select(Schedule).where(Schedule.id == schedule_id)
    )
    schedule = result.scalar_one_or_none()
    if schedule is None:
        return None
    if not can_access_schedule(schedule, user_id, is_superuser):
        return None
    # Phase 289 (D-02): project first, then serialize through the response
    # model so the cache keeps the computed runtime fields.
    await project_schedule_runtime(db, [schedule])
    if is_superuser:
        await set_cached_with_tags(
            cache_key,
            ScheduleResponse.model_validate(schedule).model_dump(),
            tags=[build_tag_key(ENTITY_TYPE)],
            ttl=CACHE_TTL_ENTITY,
        )
    return schedule


async def update_schedule(
    db: AsyncSession,
    schedule_id: uuid.UUID,
    schedule_update: ScheduleUpdate,
    current_user=None,
    *,
    user_id=None,
    is_superuser: bool = False,
) -> Schedule | None:
    result = await db.execute(
        select(Schedule).where(Schedule.id == schedule_id)
    )
    schedule = result.scalar_one_or_none()
    if not schedule:
        return None
    # 261022-np1: the ownership gate runs BEFORE template resolution/validation,
    # so a foreign id never yields a 422 that confirms the row exists. PATCH
    # never changes creator_id (the update field loop below omits it).
    if not can_access_schedule(schedule, user_id, is_superuser):
        return None

    await _resolve_template(db, schedule_update, current_user=current_user)
    await _validate_schedule_data(
        schedule_update, db, existing=schedule, current_user=current_user
    )

    for field in ["name", "job_function", "cron_expression", "args", "enabled",
                  "source_id", "schema_id", "object_type_id", "target_id",
                  "timezone", "tasktype", "preset_type", "template_id", "template_type"]:
        val = getattr(schedule_update, field, None)
        if val is not None:
            setattr(schedule, field, val)

    await db.commit()
    await db.refresh(schedule)
    await invalidate_entity_cache(ENTITY_TYPE)

    # Recompute next_run_at if cron/timezone/enabled changed
    schedule.next_run_at = _compute_next_run(
        schedule.cron_expression, schedule.timezone
    )
    await db.commit()
    await db.refresh(schedule)

    # Phase 289 (D-01/D-02): project the runtime-only status BEFORE the
    # registration sync (an unavailable plugin never registers) and re-apply it
    # after the refresh for the response.
    runtime_status, runtime_reason = await _runtime_for(db, schedule)

    # D-14: Sync update to APScheduler (no-op if scheduler not running in this process)
    await scheduler_manager.update_job(schedule)

    # Refresh to pick up next_run_at written by scheduler_manager (if it ran)
    await db.refresh(schedule)
    _apply_runtime(schedule, runtime_status, runtime_reason)

    return schedule


async def delete_schedule(
    db: AsyncSession, schedule_id: uuid.UUID, *, user_id=None, is_superuser: bool = False
) -> bool:
    result = await db.execute(
        select(Schedule).where(Schedule.id == schedule_id)
    )
    schedule = result.scalar_one_or_none()
    if not schedule:
        return False
    # 261022-np1: a foreign/creatorless schedule answers exactly like a missing
    # one, and nothing (including the APScheduler job) is touched.
    if not can_access_schedule(schedule, user_id, is_superuser):
        return False

    # D-14: Remove from APScheduler first
    await scheduler_manager.remove_job(schedule_id)

    await db.delete(schedule)
    await db.commit()
    await invalidate_entity_cache(ENTITY_TYPE)
    return True


async def preview_schedule(request: SchedulePreviewRequest) -> SchedulePreviewResponse:
    if request.preset_type is not None:
        cron = _compile_preset(request.preset_type, request.args or {})
    elif request.cron_expression is not None:
        cron = request.cron_expression
    else:
        raise ValueError("Either preset_type or cron_expression must be provided")

    try:
        CronTrigger.from_crontab(cron)
    except ValueError as e:
        raise ValueError(f"Invalid cron expression: {e}")

    human = get_description(cron)
    human_readable = f"{human} {request.timezone or 'UTC'}"
    return SchedulePreviewResponse(human_readable=human_readable)
