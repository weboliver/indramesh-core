import html
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List

import jsonschema
from sqlalchemy import select, func, or_, and_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.db.redis import get_arq_pool
from app.models.plugin import Plugin, PluginState
from app.models.service_account import ServiceAccount
from app.models.task import Task, TaskStatus, TaskType
from app.models import Import, Export

from app.schemas.crud import TaskCreate, TaskUpdate
from app.services.audit_service import audit_log, model_to_dict
from app.services.cache_service import (
    invalidate_entity_cache,
)


logger = logging.getLogger(__name__)
# Task lifecycle records are category "task" (D-16); the correlation line is the
# single API->worker join key (D-01).
task_logger = logging.getLogger("task.lifecycle")

ENTITY_TYPE = "task"

# Phase 286 (D-01/D-07): the closed Core TaskType constants. Any persisted or
# requested tasktype outside this set is plugin work and must pass the shared
# admission gate below before it can exist; Core callers are untouched.
CORE_TASKTYPE_VALUES = frozenset(t.value for t in TaskType)

# TaskUpdate.error_message is an EscapedStr(max_length=2000): html.escape()
# runs BEFORE the length check and expands up to 6x, so a raw message must be
# trimmed until its escaped form fits or the update raises a ValidationError.
ERROR_MESSAGE_MAX_LENGTH = 2000

# D-07: once one of these is written the run is over, so every accepted task-log
# line must already be durable. Kept beside the lifecycle authority that owns
# the terminal write, not inside the logging package.
TERMINAL_TASK_STATUSES = frozenset(
    {
        TaskStatus.SUCCESS.value,
        TaskStatus.PARTIAL_SUCCESS.value,
        TaskStatus.FAILED.value,
        TaskStatus.DEAD_LETTER.value,
    }
)

# D-04 creator provenance: callers that must NOT default the creator to the
# acting user pass ``creator_id`` explicitly — including an explicit ``None``,
# which marks a legacy creatorless original whose retry/schedule clone must stay
# creatorless. This sentinel distinguishes "not provided" (default to
# ``actor_id``) from "explicitly none" so a retry never infers an actor.
_CREATOR_FROM_ACTOR = object()


# Phase 300 (D-06): the fixed, user-visible reason recorded when a user cancels
# a run. The task always ends FAILED and is never retried by ARQ.
USER_CANCEL_MESSAGE = "Cancelled by user"

# Phase 300: the statuses start_task_run may transition to RUNNING. FAILED and
# DEAD_LETTER stay startable so an ARQ automatic retry of the SAME row (D-13, an
# automatic attempt, not a manual retry) still runs; SUCCESS/PARTIAL_SUCCESS and
# any row with cancel_requested_at set do not.
_STARTABLE_TASK_STATUSES = (
    TaskStatus.PENDING.value,
    TaskStatus.RUNNING.value,
    TaskStatus.FAILED.value,
    TaskStatus.DEAD_LETTER.value,
)


def safe_error_text(exc: BaseException | None, generic: str) -> str:
    """Build a Carry-03 safe error message from a caught exception.

    The raw exception text is never returned; only its type name plus a caller
    supplied generic Core message. This keeps logs and ``Task.error_message``
    free of secrets, credentials or connection strings that a library exception
    may embed (T-300-07).
    """
    name = type(exc).__name__ if exc is not None else "Error"
    return f"{name}: {generic}"


def bound_error_message(message: str, limit: int = ERROR_MESSAGE_MAX_LENGTH) -> str:
    trimmed = message[:limit]
    excess = len(html.escape(trimmed)) - limit
    while excess > 0:
        trimmed = trimmed[:-max(1, excess // 6)]
        excess = len(html.escape(trimmed)) - limit
    return trimmed


class PluginTaskAdmissionError(ValueError):
    """A plugin Task request failed the synchronous D-07/D-15 admission gate.

    Deliberately a ``ValueError`` subclass: the POST route catches it before its
    generic ``ValueError`` handler while the retry route's existing
    ``except ValueError`` already covers it, so no row is ever created for
    unknown, disabled, errored, stale-worker, malformed-parameter, or
    secret-marked-schema plugin requests.

    Phase 289 (CORESEAM-03/CORESEAM-08, D-09/D-08/D-04) splits the base class
    into the three typed causes the create/retry routes map with ONE helper:
    :class:`PluginTaskInvalidError` → 422, :class:`PluginTaskUnavailableError`
    → 409, :class:`PluginTaskPermissionError` → 403. The base class stays a
    ``ValueError`` for every existing caller.
    """


class PluginTaskInvalidError(PluginTaskAdmissionError):
    """Unknown/invalid task type or invalid start parameters → HTTP 422 (D-09)."""


class PluginTaskUnavailableError(PluginTaskAdmissionError):
    """A runtime availability conflict → HTTP 409 (D-08/D-09).

    Disabled or errored plugin, plugin not installed in this process, worker
    version drift, or a declaration ``admit`` rejection.
    """


class PluginTaskPermissionError(PluginTaskAdmissionError, PermissionError):
    """The requester may not start this task type → HTTP 403 (D-04/D-07).

    A missing/absent authenticated context, or a non-superuser without the
    declaration's ``required_permission``. Checked directly after the
    declaration resolves and BEFORE any runtime probe, so the status can never
    leak availability (T-289-04-04).
    """


@dataclass(frozen=True)
class UserAdmissionContext:
    """An explicitly authenticated requester for plugin-task admission.

    Built from the route's authenticated current-user dependency via
    :func:`user_admission_context`. It carries the SERVER-SIDE resolved
    effective permission set (deny-wins, superuser semantics preserved) — never
    a client-supplied one (T-289-04-02).
    """

    user_id: uuid.UUID
    is_superuser: bool
    permissions: frozenset[str]


@dataclass(frozen=True)
class ScheduledAdmissionContext:
    """The trusted, Core-minted scheduler-origin admission context (D-06).

    Minted ONLY by the Core scheduler's cron callback through
    :func:`trusted_scheduled_admission_context`, after loading an existing
    persisted Schedule. It bypasses ONLY the declaration
    ``required_permission`` lookup — a schedule is not bound to a creator, so a
    later role change cannot stop its automated runs. The ACTIVE lifecycle,
    worker-version, parameter and ``admit`` checks all still run before any row
    exists (D-06/D-08/D-09/D-12).
    """

    schedule_id: uuid.UUID


async def user_admission_context(current_user) -> UserAdmissionContext:
    """Build the authenticated-user admission context for a route caller.

    Superusers get the zero-query bypass (PERM-05 parity); every other user's
    effective permission set comes from the existing server-side resolver
    (``auth_service.resolve_user_permissions``, deny-wins + cache), never from
    the request body.
    """
    from app.services.auth_service import resolve_user_permissions

    user_id = uuid.UUID(str(current_user.id))
    if getattr(current_user, "is_superuser", False):
        return UserAdmissionContext(
            user_id=user_id, is_superuser=True, permissions=frozenset()
        )
    permissions = await resolve_user_permissions(str(current_user.id))
    return UserAdmissionContext(
        user_id=user_id,
        is_superuser=False,
        permissions=frozenset(permissions),
    )


def trusted_scheduled_admission_context(
    schedule_id: uuid.UUID,
) -> ScheduledAdmissionContext:
    """Mint the scheduler-only admission context (Plan 289-07's contract).

    A named factory on purpose: the ONE legitimate caller is the Core
    APScheduler callback after loading the persisted Schedule. No HTTP route,
    ``TaskCreate`` body field, ``schedule_id`` value or absent ``actor_id`` may
    produce it.
    """
    return ScheduledAdmissionContext(schedule_id=schedule_id)


_SCOPE_RECORD_ENTITY_TYPES = (
    ("source_id", "source"),
    ("schema_id", "schema"),
    ("object_type_id", "object_type"),
)


async def _require_scope_record_permissions(
    tasktype: str,
    task_data: TaskCreate,
    admission_context: UserAdmissionContext,
) -> None:
    from app.services.record_permission_service import resolve_record_permission

    for field_name, entity_type in _SCOPE_RECORD_ENTITY_TYPES:
        record_id = getattr(task_data, field_name, None)
        if record_id is None:
            continue
        resolved = await resolve_record_permission(
            user_id=str(admission_context.user_id),
            entity_type=entity_type,
            record_id=record_id,
            action="update",
        )
        if resolved is False:
            raise PluginTaskPermissionError(
                f"Not authorized to start task type {tasktype!r}: "
                f"record-level permission denied for {entity_type}:{record_id}."
            )


async def admit_plugin_task(
    db: AsyncSession,
    task_data: TaskCreate,
    *,
    admission_context: UserAdmissionContext | ScheduledAdmissionContext | None = None,
) -> None:
    """Central plugin Task admission gate, run before any row is persisted.

    No-op for a Core ``TaskType`` value so ``sync.py``, ``search.py``,
    ``import_definitions.py`` and ``scheduler_manager.py`` are unaffected. For a
    plugin-qualified literal it requires, in order:

    1. an exact declaration in THIS process's registry (D-01/D-03 — never a
       derived or aliased name; the worker process reads the worker snapshot);
    2. an explicit admission context — a missing context is NEVER automation
       (T-289-04-02). An authenticated-user context additionally requires the
       declaration's ``required_permission`` and every additive
       ``admission_permissions`` entry (D-04/D-07, D-13/D-15, superuser bypass)
       and no record-level DENY (``update``) on any supplied
       ``source_id``/``schema_id``/``object_type_id`` scope; only the trusted scheduled context skips that lookup (D-06). Checked
       before any runtime probe so a 403 cannot leak availability;
    3. a persisted plugin row whose shared lifecycle state is dispatchable
       (``ACTIVE``) — not disabled or errored (D-07);
    4. a Phase-283 worker snapshot equal to the active plugin version (D-07/
       D-12) — the ONE worker-version admission comparison; a stale worker adds
       no new work;
    5. a parameter mapping that validates against the declaration's JSON Schema
       and carries no secret marker (D-04/D-05);
    6. the declaration's optional ``admit`` hook, which may reject a start with
       a bounded :class:`TaskAdmissionRejected` reason (mapped to 409).

    Any failure raises a :class:`PluginTaskAdmissionError` subclass before
    ``db.add``; the plugin-system imports are function-local so this module
    keeps no import cycle on ``app.plugin_system``.
    """
    tasktype = task_data.tasktype
    if tasktype in CORE_TASKTYPE_VALUES:
        # Core tasks store NULL here; never attach arbitrary JSON to Core work.
        if task_data.plugin_parameters:
            raise PluginTaskInvalidError(
                "plugin_parameters are only valid for plugin task types"
            )
        return

    from app.plugin_system import validation
    from app.plugin_system.capabilities import TaskAdmissionRejected
    from app.plugin_system.task_registry import (
        RESERVED_PARAMETER_NAMES,
        get_process_task_type_registry,
    )
    from app.services.task_type_catalog import plugin_version_conflict_reason

    entry = get_process_task_type_registry().resolve(tasktype)
    if entry is None:
        raise PluginTaskInvalidError(
            f"Task type {tasktype!r} is not a declared plugin task type"
        )

    declaration = entry.declaration

    # D-04/D-06/D-07: authorize the caller for THIS declaration before probing
    # runtime availability. Only a Core-minted scheduled context skips the
    # required_permission lookup; nothing else may imply automation.
    if admission_context is None:
        raise PluginTaskPermissionError(
            f"Task type {tasktype!r} requires an authenticated admission context"
        )
    if isinstance(admission_context, UserAdmissionContext):
        if not admission_context.is_superuser:
            # D-04/D-07 + D-13/D-15: the declaration's required_permission, then
            # every additive admission_permissions entry, share the SAME 403
            # error shape and are all checked before any runtime probe so a 403
            # cannot leak availability (T-289-04-04).
            required_permissions = (
                ()
                if declaration.required_permission is None
                else (declaration.required_permission,)
            ) + tuple(declaration.admission_permissions or ())
            for permission in required_permissions:
                if permission not in admission_context.permissions:
                    raise PluginTaskPermissionError(
                        f"Not authorized to start task type {tasktype!r}: missing "
                        f"permission: {permission}."
                    )
            await _require_scope_record_permissions(
                tasktype, task_data, admission_context
            )
    elif not isinstance(admission_context, ScheduledAdmissionContext):
        raise PluginTaskPermissionError(
            f"Task type {tasktype!r} received an unknown admission context"
        )

    result = await db.execute(select(Plugin).where(Plugin.name == entry.plugin_name))
    row = result.scalar_one_or_none()
    if row is None:
        raise PluginTaskUnavailableError(
            f"Plugin {entry.plugin_name!r} for task type {tasktype!r} is not installed"
        )

    state_value = (
        row.state.value if isinstance(row.state, PluginState) else row.state
    )
    if state_value != PluginState.ACTIVE.value:
        raise PluginTaskUnavailableError(
            f"Plugin {entry.plugin_name!r} for task type {tasktype!r} is not "
            f"active (state={state_value!r})"
        )
    # D-12: the single worker-version admission comparison. The bounded wording
    # is shared with the catalog projection so the two can never drift.
    if row.worker_loaded_version != row.version:
        raise PluginTaskUnavailableError(
            f"Task type {tasktype!r}: "
            + plugin_version_conflict_reason(
                entry.plugin_name, row.worker_loaded_version, row.version
            )
        )

    schema = declaration.parameter_schema or {}
    payload = task_data.plugin_parameters or {}
    # The mapping is spread as run(ctx, task_id, **payload): a reserved key
    # would shadow a call argument even when the schema allows extra keys.
    reserved = set(payload) & RESERVED_PARAMETER_NAMES
    if reserved:
        raise PluginTaskInvalidError(
            "plugin_parameters use reserved name(s): " + ", ".join(sorted(reserved))
        )
    try:
        validation.validate_config_payload(schema, payload)
    except (
        jsonschema.exceptions.ValidationError,
        jsonschema.exceptions.SchemaError,
    ) as exc:
        raise PluginTaskInvalidError(str(exc)) from exc

    # CORESEAM-02/D-08: the plugin's optional single-flight/business admission
    # runs inside this same pre-persistence path (the Task insert commits in the
    # same transaction) for BOTH contexts — a scheduled start still reaches it.
    if declaration.admit is not None:
        try:
            await declaration.admit(db, task_data)
        except TaskAdmissionRejected as exc:
            # A plugin-authored, explicitly safe short reason (D-13 discipline).
            raise PluginTaskUnavailableError(str(exc)) from exc
        except Exception as exc:
            logger.exception("Plugin admit hook failed for task type %r", tasktype)
            raise PluginTaskUnavailableError(
                f"Task type {tasktype!r} admission check failed"
            ) from exc


# Phase 247 (R-02): bounded worker/service-only Task progress write policy —
# the first known processed count and any newly known/changed total write
# immediately; a subsequent processed change only writes once it is at least
# this many objects ahead of the last persisted value, or at least this many
# seconds have elapsed since the Task row's last update (whichever fires
# first). force=True bypasses only these two thresholds.
PROGRESS_WRITE_DELTA_THRESHOLD = 100
PROGRESS_WRITE_ELAPSED_SECONDS = 2

async def enqueue_task_job(pool, tasktype: str, task_id: str, scope, export_definition_id: str | None = None, import_definition_id: str | None = None, _job_id: str | None = None) -> None:
    """Enqueue the ARQ job for a Task, using each worker function's real positional-argument
    contract (see app/worker.py). `scope` must expose source_id/schema_id/target_id — a Task
    or a Schedule row both qualify. IMPORT/EXPORT take no scope arg here: process_import/
    process_export resolve everything from the Task's import_id/export_id, which the caller
    must have already set on the Task before calling this. For CSV exports (Phase 155),
    export_definition_id is passed through to process_export as a positional arg. A qualified
    plugin tasktype dispatches through the one generic `run_plugin_task` wrapper."""
    if tasktype == "IMPORT":
        kwargs = {"_job_id": f"import_{task_id}"}
        if import_definition_id:
            kwargs["import_definition_id"] = import_definition_id
        await pool.enqueue_job("process_import", task_id, **kwargs)
    elif tasktype == "EXPORT":
        if export_definition_id:
            await pool.enqueue_job(
                "process_export", task_id, export_definition_id,
                _job_id=f"export_{task_id}",
            )
        else:
            await pool.enqueue_job(
                "process_export", task_id,
                _job_id=f"export_{task_id}",
            )
    elif tasktype == "REINDEX":
        if scope.schema_id is None:
            await pool.enqueue_job(
                "reindex_all_job", task_id, _job_id=f"reindex_all_{task_id}"
            )
        else:
            await pool.enqueue_job(
                "schema_reindex_job", str(scope.schema_id), task_id, _job_id=f"reindex_schema_{task_id}"
            )
    else:
        # Phase 286 (PLUGTASK-03/PLUGTASK-04): a declared plugin task type
        # dispatches through the one static Core-owned generic worker wrapper.
        # The worker-local task-type registry resolves the persisted literal to
        # its plugin runner at execution time; the backend never appends plugin
        # callables to WorkerSettings.functions. Core job names/positional
        # contracts above are untouched.
        await pool.enqueue_job(
            "run_plugin_task", task_id, _job_id=_job_id or f"plugin_{task_id}"
        )


async def create_task(
    db: AsyncSession,
    task_data: TaskCreate,
    actor_id: uuid.UUID | None = None,
    *,
    creator_id: uuid.UUID | None = _CREATOR_FROM_ACTOR,
    admission_context: UserAdmissionContext | ScheduledAdmissionContext | None = None,
    trigger: str | None = None,
) -> Task:
    """Create a Task row.

    D-04 creator provenance: by default the persisted ``creator_id`` is the
    authenticated ``actor_id`` that admitted this task (the POST /tasks and
    scheduled-with-actor paths). A caller that derives the creator from a
    different row — ``retry_task`` (the original task's creator) or the
    scheduler (the permanent template's creator) — passes ``creator_id``
    explicitly. ``creator_id=None`` is a real, intentional value: a legacy
    creatorless original keeps its clone creatorless so the SDK write later
    fails closed instead of guessing the retrier/scheduler as the actor.
    """
    if task_data.parent_task_id:
        result = await db.execute(
            select(Task).where(Task.id == task_data.parent_task_id)
        )
        parent = result.scalar_one_or_none()
        if not parent:
            raise ValueError("Parent task not found")

    if task_data.import_id:
        result = await db.execute(
            select(Import).where(Import.id == task_data.import_id)
        )
        import_obj = result.scalar_one_or_none()
        if not import_obj:
            raise ValueError("Import not found")

    if task_data.export_id:
        result = await db.execute(
            select(Export).where(Export.id == task_data.export_id)
        )
        export_obj = result.scalar_one_or_none()
        if not export_obj:
            raise ValueError("Export not found")

    # Phase 286 (D-07/D-15): the shared admission gate runs on EVERY creation
    # path before the row exists — POST /tasks, retry_task and every service
    # caller — so no undispatchable plugin work can be created around it. A
    # Core tasktype is a no-op and ignores the context entirely.
    await admit_plugin_task(db, task_data, admission_context=admission_context)

    resolved_creator_id = (
        actor_id if creator_id is _CREATOR_FROM_ACTOR else creator_id
    )
    db_task = Task(
        parent_task_id=task_data.parent_task_id,
        import_id=task_data.import_id,
        export_id=task_data.export_id,
        schedule_id=task_data.schedule_id,
        source_id=task_data.source_id,
        schema_id=task_data.schema_id,
        object_type_id=task_data.object_type_id,
        target_id=task_data.target_id,
        export_definition_id=task_data.export_definition_id,
        import_definition_id=task_data.import_definition_id,
        service_account_id=task_data.service_account_id,
        current_step=task_data.current_step,
        current_status=task_data.current_status,
        sort=task_data.sort,
        tasktype=task_data.tasktype,
        plugin_parameters=task_data.plugin_parameters,
        name=task_data.name,
        error_message=task_data.error_message,
        started_at=task_data.started_at,
        completed_at=task_data.completed_at,
        is_permanent=task_data.is_permanent,
        creator_id=resolved_creator_id,
    )
    db.add(db_task)
    await db.flush()
    await audit_log(db, "tasks", db_task.id, "CREATE", actor_id,
                    old_values=None, new_values=model_to_dict(db_task))
    await db.commit()
    await db.refresh(db_task)

    # D-01: exactly one correlation line per created task. request_id reaches it
    # through the context filter when created inside an HTTP request; the worker
    # joins on task_id. Explicit trigger wins, otherwise derive from the row.
    resolved_trigger = trigger or ("schedule" if db_task.schedule_id else "manual")
    created_extra: dict[str, str] = {
        "task_id": str(db_task.id),
        "task_type": db_task.tasktype,
        "trigger": resolved_trigger,
    }
    if db_task.schema_id is not None:
        created_extra["schema_id"] = str(db_task.schema_id)
    if db_task.schedule_id is not None:
        created_extra["schedule_id"] = str(db_task.schedule_id)
    task_logger.info("task created", extra=created_extra)

    await invalidate_entity_cache(ENTITY_TYPE)
    return db_task


async def list_tasks(
    db: AsyncSession, skip: int = 0, limit: int = 100, task_type: str | None = None,
    schedule_id_null: bool | None = None,
    is_permanent: bool | None = None,
    current_status: str | None = None,
    *,
    creator_id=None,
    visible_export_ids=None,
    permanent_owner_id=None,
) -> tuple[List[Task], int]:
    """List tasks, optionally scoped to the caller's own export runs.

    ``creator_id`` (261022-np1) restricts both the count and the items to rows
    whose server-authored ``Task.creator_id`` equals it — used by the Create
    Schedule template picker so a caller only ever sees their own permanent
    templates. ``None`` means "no restriction".

    ``visible_export_ids`` is a select of Export ids, or ``None`` for "no
    restriction" (internal callers and superusers). When it is set, one extra
    predicate applies to both the count and the item query: non-EXPORT tasks stay
    visible; an EXPORT run is visible only through an export in the set; a
    permanent EXPORT template is visible only to the caller who created it
    (``permanent_owner_id``, 261022-np1), and a permanent template with no owner
    is superuser-only — fail closed, so a foreign template never leaks.
    """
    query = select(Task)
    if task_type:
        query = query.where(Task.tasktype == task_type)

    count_query = select(func.count()).select_from(Task)
    if task_type:
        count_query = count_query.where(Task.tasktype == task_type)

    if current_status:
        query = query.where(Task.current_status == current_status)
        count_query = count_query.where(Task.current_status == current_status)

    if schedule_id_null is True:
        query = query.where(Task.schedule_id.is_(None))
        count_query = count_query.where(Task.schedule_id.is_(None))
    elif schedule_id_null is False:
        query = query.where(Task.schedule_id.is_not(None))
        count_query = count_query.where(Task.schedule_id.is_not(None))

    if is_permanent is not None:
        query = query.where(Task.is_permanent == is_permanent)
        count_query = count_query.where(Task.is_permanent == is_permanent)

    if creator_id is not None:
        query = query.where(Task.creator_id == creator_id)
        count_query = count_query.where(Task.creator_id == creator_id)

    if visible_export_ids is not None:
        scope_terms = [
            Task.tasktype != TaskType.EXPORT.value,
            Task.export_id.in_(visible_export_ids),
        ]
        if permanent_owner_id is not None:
            # A permanent EXPORT template is a schedule template, not a run: it
            # belongs to the user who created it. Without an owner it stays
            # superuser-only (fail closed).
            scope_terms.append(
                and_(
                    Task.is_permanent.is_(True),
                    Task.creator_id == permanent_owner_id,
                )
            )
        export_scope = or_(*scope_terms)
        query = query.where(export_scope)
        count_query = count_query.where(export_scope)

    count_result = await db.execute(count_query)
    total = count_result.scalar()
    result = await db.execute(
        query.order_by(Task.created_at.desc()).offset(skip).limit(limit)
    )
    items = result.scalars().all()
    return items, total


async def get_task(
    db: AsyncSession, task_id: uuid.UUID
) -> Task | None:
    result = await db.execute(
        select(Task).where(Task.id == task_id)
    )
    return result.scalar_one_or_none()


@dataclass(frozen=True)
class TaskCredentials:
    """The narrow, task-bound ServiceAccount credential projection (CORESEAM-04).

    Returned only by :func:`resolve_task_credentials` — exactly the fields a
    connector task legitimately needs, already decrypted through the existing
    ``EncryptedString`` path. No account id, config or other row field is
    exposed, so the object cannot be used to reach a different account.
    """

    username: str | None
    source_bearer_token: str | None
    target_bearer_token: str | None
    expires_at: datetime | None

    def __repr__(self) -> str:
        """Mask both bearer tokens (review 2).

        A dataclass-defined ``__repr__`` takes precedence over the generated one,
        so an accidental ``repr()``/log/format of this object can never leak a
        decrypted value (T-289-05-01).
        """

        def _masked(value: str | None) -> str | None:
            return None if value is None else "***"

        return (
            f"TaskCredentials(username={self.username!r}, "
            f"source_bearer_token={_masked(self.source_bearer_token)!r}, "
            f"target_bearer_token={_masked(self.target_bearer_token)!r}, "
            f"expires_at={self.expires_at!r})"
        )


async def resolve_task_credentials(
    db: AsyncSession, task_id: uuid.UUID
) -> TaskCredentials:
    """Resolve the credentials of the ServiceAccount bound to ``task_id``.

    The task association is the ONLY lookup input (D-04/T-289-05-01): there is
    deliberately no account-id parameter, so a caller cannot select an account.
    Every refusal is a Core-authored, bounded message that names no decrypted
    value — ``PluginContext.credentials`` turns it into a safe
    ``PluginTaskError`` for the task's error_message.
    """
    task = await get_task(db, task_id)
    if task is None:
        raise ValueError("Task not found — credentials cannot be resolved")
    if task.service_account_id is None:
        raise ValueError(
            "Task has no service account bound — plugin credentials are "
            "task-bound and this task carries no association"
        )
    result = await db.execute(
        select(ServiceAccount).where(ServiceAccount.id == task.service_account_id)
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise ValueError(
            "The service account bound to this task no longer exists"
        )
    if not account.is_active:
        raise ValueError("The service account bound to this task is inactive")
    # Review 2: an EXPIRED account is refused exactly like an inactive one. The
    # stored timestamp is naive UTC (the model's convention), so compare against
    # the same clock shape rather than a tz-aware one.
    if account.expires_at is not None and account.expires_at < datetime.now(
        timezone.utc
    ).replace(tzinfo=None):
        raise ValueError("The service account bound to this task is expired")
    return TaskCredentials(
        username=account.username,
        source_bearer_token=account.source_bearer_token,
        target_bearer_token=account.target_bearer_token,
        expires_at=account.expires_at,
    )


async def update_task(
    db: AsyncSession,
    task_id: uuid.UUID,
    task_update: TaskUpdate,
    actor_id: uuid.UUID | None = None,
    *,
    clear_error_message: bool = False,
    reset_progress: bool = False,
) -> Task | None:
    # clear_error_message is a service/worker-only switch: TaskUpdate cannot
    # express "set error_message to NULL" (None means "leave unchanged"), so a
    # succeeding re-run uses it to drop the previous attempt's error text.
    # reset_progress is the same kind of switch for the progress counters: a
    # retried attempt of the same row starts a fresh monotonic baseline.
    from app.logging.task_log import get_task_log_writer

    result = await db.execute(
        select(Task).where(Task.id == task_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        return None

    old_values = model_to_dict(task)

    if task_update.parent_task_id is not None:
        task.parent_task_id = task_update.parent_task_id
    if task_update.import_id is not None:
        task.import_id = task_update.import_id
    if task_update.export_id is not None:
        task.export_id = task_update.export_id
    if task_update.target_id is not None:
        task.target_id = task_update.target_id
    if task_update.export_definition_id is not None:
        task.export_definition_id = task_update.export_definition_id
    if task_update.import_definition_id is not None:
        task.import_definition_id = task_update.import_definition_id
    if task_update.service_account_id is not None:
        task.service_account_id = task_update.service_account_id
    if task_update.current_step is not None:
        # D-14: a real step change starts a new section, recorded at the moment
        # the step changes rather than when the buffered lines are flushed.
        if task_update.current_step != task.current_step:
            _writer = get_task_log_writer()
            if _writer is not None:
                _writer.set_step(task_id, task_update.current_step)
        task.current_step = task_update.current_step
    if task_update.current_status is not None:
        task.current_status = task_update.current_status
    if task_update.sort is not None:
        task.sort = task_update.sort
    # Phase 286 (D-01/D-03): tasktype identity is immutable across the Core /
    # plugin boundary. A plugin task's admitted literal can never be rewritten,
    # and a Core task can never be converted into plugin work outside the
    # admission gate; Core-to-Core stays unchanged. Rejected before flush.
    if task_update.tasktype is not None:
        if (
            task.tasktype not in CORE_TASKTYPE_VALUES
            or task_update.tasktype not in CORE_TASKTYPE_VALUES
        ):
            raise ValueError(
                "tasktype cannot be changed to or from a plugin task type"
            )
        task.tasktype = task_update.tasktype
    if task_update.name is not None:
        task.name = task_update.name
    if task_update.error_message is not None:
        task.error_message = task_update.error_message
    elif clear_error_message:
        task.error_message = None
    if reset_progress:
        task.processed_objects = None
        task.total_objects = None
    if task_update.started_at is not None:
        task.started_at = task_update.started_at
    if task_update.completed_at is not None:
        task.completed_at = task_update.completed_at
    if task_update.is_permanent is not None:
        task.is_permanent = task_update.is_permanent

    # D-07 barrier: an accepted task-log line must be durable before a terminal
    # status becomes visible. Runs before the local flush so the job session
    # holds no row lock while the writer waits on the database.
    if task_update.current_status is not None:
        status_value = getattr(
            task_update.current_status, "value", task_update.current_status
        )
        if status_value in TERMINAL_TASK_STATUSES:
            _writer = get_task_log_writer()
            if _writer is not None:
                await _writer.flush_task(task_id)

    await db.flush()
    await audit_log(db, "tasks", task.id, "UPDATE", actor_id,
                    old_values=old_values, new_values=model_to_dict(task))
    await db.commit()
    await db.refresh(task)
    await invalidate_entity_cache(ENTITY_TYPE)
    return task


def _utcnow() -> datetime:
    """Sole time source for update_task_progress's elapsed-threshold check —
    a module-level seam so tests can monkeypatch app.services.task_service._utcnow
    for deterministic threshold assertions instead of real sleeps (Phase 247
    Plan 01 Task 2)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def update_task_progress(
    db: AsyncSession,
    task_id: uuid.UUID,
    processed_objects: int | None = None,
    total_objects: int | None = None,
    force: bool = False,
) -> Task | None:
    """Worker/service-only bounded Task progress writer (Phase 247, R-02).

    NOT a lifecycle/audited write — see update_task() for that; this helper
    writes no audit entry. Accepts only server-observed counts: NULL input
    retains an already-known persisted fact (never clears it), negative or
    regressing values are rejected, and a known total below a known
    processed value is rejected. A real write only actually happens at the
    first known processed count, any newly known or changed total, or once
    a subsequent processed change is at least PROGRESS_WRITE_DELTA_THRESHOLD
    objects ahead of the last persisted value or at least
    PROGRESS_WRITE_ELAPSED_SECONDS have elapsed since the Task row's last
    update. force=True bypasses only those two thresholds for an exact,
    monotonic terminal pair — it never bypasses the validation above.

    Each real write directly flushes/commits/refreshes the Task and
    invalidates the Task cache exactly once, with no audit entry. A
    suppressed call performs none of those side effects and returns the
    Task's current (unchanged) persisted state.
    """
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        return None

    if processed_objects is not None and processed_objects < 0:
        raise ValueError("processed_objects must not be negative")
    if total_objects is not None and total_objects < 0:
        raise ValueError("total_objects must not be negative")
    if (
        processed_objects is not None
        and task.processed_objects is not None
        and processed_objects < task.processed_objects
    ):
        raise ValueError("processed_objects must not regress below the persisted value")
    if (
        total_objects is not None
        and task.total_objects is not None
        and total_objects < task.total_objects
    ):
        raise ValueError("total_objects must not regress below the persisted value")

    effective_processed = processed_objects if processed_objects is not None else task.processed_objects
    effective_total = total_objects if total_objects is not None else task.total_objects
    if (
        effective_total is not None
        and effective_processed is not None
        and effective_total < effective_processed
    ):
        raise ValueError("total_objects must not be below processed_objects")

    processed_is_first = processed_objects is not None and task.processed_objects is None
    total_is_new = total_objects is not None and total_objects != task.total_objects
    processed_changed = (
        processed_objects is not None
        and task.processed_objects is not None
        and processed_objects != task.processed_objects
    )

    should_write = force or processed_is_first or total_is_new

    if not should_write and processed_changed:
        delta = processed_objects - task.processed_objects
        elapsed = None
        if task.updated_at is not None:
            elapsed = (_utcnow() - task.updated_at).total_seconds()
        if delta >= PROGRESS_WRITE_DELTA_THRESHOLD or (
            elapsed is not None and elapsed >= PROGRESS_WRITE_ELAPSED_SECONDS
        ):
            should_write = True

    if not should_write:
        return task

    if processed_objects is not None:
        task.processed_objects = processed_objects
    if total_objects is not None:
        task.total_objects = total_objects
    # Explicit stamp (rather than relying on the model's onupdate=lambda,
    # which uses the real wall clock unconditionally) so the elapsed-time
    # threshold above is driven entirely by _utcnow() and stays deterministic
    # under a monkeypatched clock in tests. flag_modified is required: if
    # _utcnow() happens to return the same value already on the row (e.g. no
    # time advanced between two real writes), SQLAlchemy's history tracking
    # sees no attribute change and would fall back to the column's onupdate
    # default (real wall clock) instead of honoring this explicit value.
    task.updated_at = _utcnow()
    flag_modified(task, "updated_at")

    await db.flush()
    await db.commit()
    await db.refresh(task)
    await invalidate_entity_cache(ENTITY_TYPE)
    return task


async def delete_task(
    db: AsyncSession, task_id: uuid.UUID, actor_id: uuid.UUID | None = None
) -> bool:
    result = await db.execute(
        select(Task).where(Task.id == task_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        return False
    old_values = model_to_dict(task)
    await audit_log(db, "tasks", task.id, "DELETE", actor_id,
                    old_values=old_values, new_values=None)
    await db.delete(task)
    await db.commit()
    await invalidate_entity_cache(ENTITY_TYPE)
    return True


async def retry_task(
    db: AsyncSession,
    task_id: uuid.UUID,
    actor_id: uuid.UUID | None = None,
    *,
    admission_context: UserAdmissionContext | ScheduledAdmissionContext | None = None,
) -> Task | None:
    task = await get_task(db, task_id)
    if not task:
        return None

    retryable = (TaskStatus.FAILED.value, TaskStatus.DEAD_LETTER.value)
    if task.current_status not in retryable:
        raise ValueError("Can only retry failed tasks")
    import_id = task.import_id
    export_id = task.export_id
    tasktype = task.tasktype
    source_id = task.source_id
    schema_id = task.schema_id
    object_type_id = task.object_type_id
    target_id = task.target_id
    export_definition_id = task.export_definition_id
    import_definition_id = task.import_definition_id
    service_account_id = task.service_account_id
    # D-04: the retry inherits the ORIGINAL task's creator as the new row's
    # creator — the user triggering the retry (``actor_id``) stays only the
    # CREATE audit initiator and never replaces the SDK-write actor. A legacy
    # original with no creator yields a creatorless clone (explicit None), so
    # its task-bound SDK writes fail closed rather than attributing to the
    # retrier.
    original_creator_id = task.creator_id
    # Phase 286 (D-06): the admitted plugin input is copied verbatim so a
    # retried task reproduces the original run's input byte-for-byte.
    plugin_parameters = task.plugin_parameters
    # Phase 286 (D-10): a plugin task is created with no business step and keeps
    # that on retry even when the failed attempt already reported one; only a
    # Core task resets to SCHEMA. Keyed off the task type, never the persisted
    # step value (a reported plugin step is non-NULL too).
    initial_step = "SCHEMA" if tasktype in CORE_TASKTYPE_VALUES else None

    # CSV import retry guard — the uploaded file (temp_file_path) is deleted
    # after processing and can never be re-derived from the Task record alone.
    # Without this guard, retrying a definition-backed IMPORT would create a row
    # the Core import pipeline cannot serve (no retained payload) and fail
    # instead of re-running the original work.
    if tasktype == "IMPORT" and import_definition_id:
        raise ValueError(
            "CSV import retry not supported — the uploaded file is not retained; "
            "re-run the import via POST /imports/csv/upload with a new file"
        )

    task_data = TaskCreate(
        import_id=import_id,
        export_id=export_id,
        tasktype=tasktype,
        source_id=source_id,
        schema_id=schema_id,
        object_type_id=object_type_id,
        target_id=target_id,
        export_definition_id=export_definition_id,
        import_definition_id=import_definition_id,
        service_account_id=service_account_id,
        plugin_parameters=plugin_parameters,
        current_step=initial_step,
        current_status="PENDING",
        error_message=None,
        started_at=None,
        completed_at=None,
    )
    new_task = await create_task(
        db, task_data, actor_id=actor_id, creator_id=original_creator_id,
        admission_context=admission_context, trigger="retry",
    )
    # Phase 289 (D-07): the retry forwards the CURRENT caller's admission
    # context — a retry is a new start, so a previously authorized Task never
    # authorizes its own retry after the caller's role changes.

    arq = await get_arq_pool()
    await enqueue_task_job(
        arq, tasktype, str(new_task.id), new_task,
        export_definition_id=str(export_definition_id) if export_definition_id else None,
        import_definition_id=str(import_definition_id) if import_definition_id else None,
    )

    return new_task


async def mark_task_failed_or_dead_letter(
    db: AsyncSession, task_id: uuid.UUID, error_msg: str, ctx: dict | None, max_tries: int = 5
) -> None:
    """Distinguish a transient job failure (ARQ will still retry) from a final one.

    ARQ exposes the current attempt number as ctx['job_try']. Once that
    reaches max_tries this was the last attempt, so the task is marked
    DEAD_LETTER instead of FAILED — surfacing it for manual replay rather
    than silently vanishing once ARQ's retry budget runs out.
    """
    job_try = (ctx or {}).get("job_try", 1)
    status = TaskStatus.DEAD_LETTER.value if job_try >= max_tries else TaskStatus.FAILED.value
    # Backstop for every caller (MJ-02): an oversized message must never turn
    # the terminal write into a ValidationError that leaves the task RUNNING.
    task_update = TaskUpdate(
        current_status=status,
        error_message=bound_error_message(error_msg) if error_msg else error_msg,
        completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    await update_task(db, task_id, task_update)


async def _request_running_cancel(
    db: AsyncSession, task: Task, actor_id: uuid.UUID | None, now: datetime
) -> None:
    """Record a cancel REQUEST on a RUNNING task, idempotently (D-05).

    Status and ``completed_at`` stay untouched — the worker owns the terminal
    write after it has stopped the run and flushed the logs.
    """
    if task.cancel_requested_at is not None:
        return
    old_values = model_to_dict(task)
    task.cancel_requested_at = now
    await db.flush()
    await audit_log(db, "tasks", task.id, "UPDATE", actor_id,
                    old_values=old_values, new_values=model_to_dict(task))
    await db.commit()
    await db.refresh(task)
    await invalidate_entity_cache(ENTITY_TYPE)


async def cancel_task(
    db: AsyncSession, task_id: uuid.UUID, actor_id: uuid.UUID | None = None
) -> Task | None:
    """Request cancellation of a pending or running task (D-05/D-06).

    * RUNNING → only ``cancel_requested_at`` is set; the status stays RUNNING
      until the worker notices the request, stops the run, flushes the logs and
      writes FAILED. A second cancel changes nothing.
    * PENDING → a conditional update (``WHERE current_status = 'PENDING'``)
      fails it immediately with "Cancelled by user", a ``cancel_requested_at``
      marker, and a "Cancelled before start" log line in the same transaction.
      If a worker has already started it, the conditional update misses and the
      request is recorded exactly like the RUNNING case.
    """
    from app.services.task_log_service import append_task_log

    task = await get_task(db, task_id)
    if not task:
        return None

    if task.current_status not in (TaskStatus.RUNNING, TaskStatus.PENDING):
        raise ValueError("Can only cancel running or pending tasks")

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    if task.current_status == TaskStatus.PENDING:
        result = await db.execute(
            update(Task)
            .where(Task.id == task_id, Task.current_status == TaskStatus.PENDING.value)
            .values(
                current_status=TaskStatus.FAILED.value,
                error_message=USER_CANCEL_MESSAGE,
                completed_at=now,
                cancel_requested_at=now,
            )
        )
        if (result.rowcount or 0) == 0:
            # A worker started the task between our read and the conditional
            # update — fall back to a cancel request.
            await _request_running_cancel(db, task, actor_id, now)
            return task
        await append_task_log(
            db, task_id, logging.WARNING, "Cancelled before start"
        )
        await audit_log(db, "tasks", task_id, "UPDATE", actor_id,
                        old_values=None, new_values=model_to_dict(task))
        await db.commit()
        await db.refresh(task)
        await invalidate_entity_cache(ENTITY_TYPE)
        return task

    await _request_running_cancel(db, task, actor_id, now)
    return task


async def start_task_run(
    db: AsyncSession, task_id: uuid.UUID, *, reset_progress: bool = False
) -> bool:
    """Conditionally transition a task to RUNNING for a real run (D-05).

    Returns False when the task no longer exists, was already cancelled (in
    which case it is finalized as FAILED "Cancelled by user" with the D-07
    flush barrier), or had already finished successfully. A previously FAILED /
    DEAD_LETTER row stays startable so an ARQ automatic retry of the same row
    (D-13) runs as its own attempt. Callers must do no work when this returns
    False.
    """
    now = _utcnow()
    values: dict = {
        "current_status": TaskStatus.RUNNING.value,
        "started_at": func.coalesce(Task.started_at, now),
        "updated_at": now,
    }
    if reset_progress:
        values["processed_objects"] = None
        values["total_objects"] = None

    result = await db.execute(
        update(Task)
        .where(
            Task.id == task_id,
            Task.current_status.in_(_STARTABLE_TASK_STATUSES),
            Task.cancel_requested_at.is_(None),
        )
        .values(**values)
    )
    if (result.rowcount or 0) == 0:
        task = await get_task(db, task_id)
        if (
            task is not None
            and task.cancel_requested_at is not None
            and task.current_status not in TERMINAL_TASK_STATUSES
        ):
            await update_task(
                db,
                task_id,
                TaskUpdate(
                    current_status=TaskStatus.FAILED.value,
                    error_message=USER_CANCEL_MESSAGE,
                    completed_at=now,
                ),
            )
        return False

    # D-07/TLOG-01: guarantee every started task run has at least one durable
    # lifecycle line, so a run's log is never empty (the core jobs themselves
    # emit progress/errors only when they occur). Committed atomically with the
    # RUNNING transition.
    from app.services.task_log_service import append_task_log

    await append_task_log(
        db,
        task_id,
        logging.INFO,
        "Task run started",
        category="task",
        logger_name="task.lifecycle",
    )
    await db.commit()
    await invalidate_entity_cache(ENTITY_TYPE)
    return True
