"""SchedulerManager — APScheduler integration singleton.

Runs inside the worker container alongside ARQ (per D-12).
Uses AsyncIOScheduler with SQLAlchemyJobStore pointing to PostgreSQL (per D-13).
"""

import uuid
import logging
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from sqlalchemy import select, update, case, cast, DateTime

from app.db import async_session_maker
from app.db.redis import get_arq_pool
from app.logging.worker import with_scheduler_log_context
from app.models.schedule import Schedule
from app.models.task import Task, TaskStatus, TaskType, TaskStep
from app.models.export_definition import ExportDefinition
from app.models.object_type import ObjectType
from app.schemas.crud import TaskCreate
from app.services import task_service


logger = logging.getLogger(__name__)
# Job-lifecycle/scheduler lines are category "task" (D-16); the existing
# `logger` name is kept for the pre-existing scheduler messages (D-18).
task_logger = logging.getLogger("task.scheduler")

# D-05: Function name registry — maps string names to callables
# D-06: All existing ARQ job functions available for scheduling
JOB_REGISTRY: dict[str, Callable] = {}


def init_job_registry():
    """Lazy import and populate JOB_REGISTRY to avoid circular imports."""
    from app.worker import (
        process_import, process_export,
        index_object_job, remove_object_job, bulk_reindex_job,
        schema_reindex_job, reindex_all_job,
    )
    JOB_REGISTRY["process_import"] = process_import
    JOB_REGISTRY["process_export"] = process_export
    JOB_REGISTRY["index_object_job"] = index_object_job
    JOB_REGISTRY["remove_object_job"] = remove_object_job
    JOB_REGISTRY["bulk_reindex_job"] = bulk_reindex_job
    JOB_REGISTRY["schema_reindex_job"] = schema_reindex_job
    JOB_REGISTRY["reindex_all_job"] = reindex_all_job


class SchedulerManager:
    """Manages APScheduler lifecycle and syncs with DB state."""

    def __init__(self):
        self._scheduler: AsyncIOScheduler | None = None

    async def start(self):
        """Initialize AsyncIOScheduler with SQLAlchemyJobStore and load enabled schedules."""
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
        from app.db import engine

        # SQLAlchemyJobStore needs a sync engine — APScheduler 3.x doesn't support async engines
        # directly. Use the sync URL equivalent. str(engine.url) masks the password as '***'
        # since SQLAlchemy 1.4+, so render_as_string(hide_password=False) is required here.
        from sqlalchemy import create_engine
        sync_url = engine.url.render_as_string(hide_password=False).replace("+asyncpg", "")
        # D-19: bound parameters must never appear in StatementError text.
        sync_engine = create_engine(sync_url, hide_parameters=True)

        jobstore = SQLAlchemyJobStore(engine=sync_engine)
        self._scheduler = AsyncIOScheduler(
            jobstores={"default": jobstore},
            timezone="UTC",  # D-03
        )
        self._scheduler.start()

        # Purge stale jobs from previous runs — _load_enabled_schedules re-adds them.
        # APScheduler's next_run_time is tz-aware, but the DB column is TIMESTAMP WITHOUT
        # TIME ZONE, causing DataError on replace_existing. Clearing first avoids the conflict.
        for job in self._scheduler.get_jobs():
            if job.id.startswith("schedule_"):
                self._scheduler.remove_job(job.id)

        # Internal system job (not user-configurable, no Task record): re-enqueues
        # index_outbox entries stuck in PENDING/ENQUEUED — the self-healing half
        # of the transactional outbox pattern for ES indexing. Runs through the
        # module-level wrapper so every line carries trigger=schedule and a
        # freshly reset context.
        self._scheduler.add_job(
            _run_outbox_sweeper,
            trigger="interval",
            minutes=2,
            id="system_outbox_sweeper",
            replace_existing=True,
            name="Index Outbox Sweeper",
        )

        # Phase 300 (D-11/D-12): delete task-log lines older than
        # TASK_LOG_RETENTION_DAYS once at start and then hourly. Runs with no
        # task context; it never touches PENDING/RUNNING tasks and never deletes
        # the Task row.
        self._register_task_log_purge()

        # Load enabled schedules from DB
        schedules = await self._load_enabled_schedules()
        logger.info("SchedulerManager started with %d enabled schedule(s)", len(schedules))

        # Periodic refresh: pick up schedules created/updated via the API
        self._scheduler.add_job(
            _refresh_schedules,
            trigger="interval",
            minutes=1,
            id="system_schedule_refresh",
            replace_existing=True,
            name="Schedule DB Refresh",
        )

    async def shutdown(self):
        """Gracefully shut down APScheduler."""
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("SchedulerManager shut down")

    def _register_task_log_purge(self) -> None:
        """Register the hourly task-log retention job (runs once at start)."""
        self._scheduler.add_job(
            _run_task_log_purge,
            trigger="interval",
            hours=1,
            id="system_task_log_purge",
            replace_existing=True,
            name="Task Log Retention Purge",
            next_run_time=datetime.now(timezone.utc),
        )

    async def _load_enabled_schedules(self):
        """D-15: Load all enabled schedules from DB and register with APScheduler.

        Phase 289 (289-07, D-01/D-03): dispatchability is resolved per pass with
        the SAME persisted-truth predicate the API's ``runtime_status`` uses. A
        paused schedule (plugin unavailable / stale worker) registers no job
        and gets NULL persisted for next_run_at — ``Schedule.enabled`` is never
        touched, so the user's intent survives the outage.
        """
        from app.services.schedule_service import (
            RUNTIME_STATUS_PAUSED,
            RUNTIME_STATUS_SCHEDULED,
            schedule_runtime_states,
        )

        async with async_session_maker() as session:
            result = await session.execute(
                select(Schedule).where(Schedule.enabled == True)
            )
            schedules = result.scalars().all()
            # T-289-07-03: a resolution failure must never abort scheduler
            # startup — fall back to today's register-everything behaviour.
            try:
                states = await schedule_runtime_states(session, schedules)
            except Exception:
                logger.exception(
                    "SchedulerManager: runtime resolution failed; registering "
                    "every loaded schedule (fail-open)"
                )
                states = {}
            task_logger.info("scheduler: loading %d enabled schedule(s)", len(schedules))
            for schedule in schedules:
                if (
                    states.get(schedule.id, (RUNTIME_STATUS_SCHEDULED, None))[0]
                    == RUNTIME_STATUS_PAUSED
                ):
                    # D-01/D-03: runtime pause only — no job (never pause_job(),
                    # which a resume would replay from its misfire window) and
                    # no enabled=false write.
                    self._remove_schedule_job(schedule.id)
                    task_logger.info(
                        "scheduler: runtime-paused schedule %s (%s)",
                        schedule.id,
                        schedule.name,
                    )
                    continue
                try:
                    await self._add_to_scheduler(schedule)
                    task_logger.debug(
                        "scheduler: registered schedule %s (%s)",
                        schedule.id,
                        schedule.name,
                    )
                except Exception:
                    task_logger.exception(
                        "scheduler: failed to register schedule %s", schedule.id
                    )
            # D-01/D-02: persist next_run_at for ALL loaded schedules in one batched
            # UPDATE instead of N per-row session+SELECT+UPDATE+COMMIT writes.
            # D-03: every loaded schedule contributes a pair — a schedule with no
            # registered job (failed registration above) maps to None -> NULL,
            # preserving today's "second loop iterates ALL schedules" semantics.
            pairs: dict[uuid.UUID, datetime | None] = {
                schedule.id: _read_next_run_at(self, schedule.id) for schedule in schedules
            }
            await self._persist_next_run_at_batch(pairs)
            return schedules

    async def add_job(self, schedule: Schedule):
        """Add a job to APScheduler. Called after schedule creation.

        Phase 289 (289-07, D-01): a runtime-paused schedule (plugin unavailable
        / stale worker) never registers a job. ``schedule_service`` projects
        ``runtime_status`` from persisted truth immediately before this call;
        the worker's startup/refresh pass re-registers it once the plugin and
        worker are dispatchable again. An absent projection (pre-289 caller)
        keeps the previous register behaviour.
        """
        if not self._scheduler or not schedule.enabled:
            return
        if _runtime_paused(schedule):
            return
        await self._add_to_scheduler(schedule)
        await self._update_next_run_at(schedule)

    async def update_job(self, schedule: Schedule):
        """Update an existing job in APScheduler. Reschedule if cron/enabled changed."""
        if not self._scheduler:
            return
        job_id = self._build_job_id(schedule.id)
        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)
        if schedule.enabled and not _runtime_paused(schedule):
            await self._add_to_scheduler(schedule)
        await self._update_next_run_at(schedule)

    async def remove_job(self, schedule_id: uuid.UUID):
        """Remove a job from APScheduler. Called after schedule deletion."""
        if not self._scheduler:
            return
        job_id = self._build_job_id(schedule_id)
        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)

    def _remove_schedule_job(self, schedule_id: uuid.UUID) -> None:
        """Remove a schedule's APScheduler job if one is registered.

        Phase 289 (289-07, D-01/D-03): this is the ONLY pause mechanism. The
        registered jobs carry ``misfire_grace_time=900, coalesce=True``, so
        ``pause_job()``+``resume_job()`` would replay a missed occurrence —
        removing the job and re-adding a fresh CronTrigger on recovery
        guarantees the next regular occurrence only.
        """
        if not self._scheduler:
            return
        job_id = self._build_job_id(schedule_id)
        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)

    async def pause_job(self, schedule_id: uuid.UUID):
        """Pause a scheduled job (disable)."""
        if not self._scheduler:
            return
        job_id = self._build_job_id(schedule_id)
        if self._scheduler.get_job(job_id):
            self._scheduler.pause_job(job_id)

    async def resume_job(self, schedule_id: uuid.UUID):
        """Resume a paused scheduled job (enable)."""
        if not self._scheduler:
            return
        job_id = self._build_job_id(schedule_id)
        if self._scheduler.get_job(job_id):
            self._scheduler.resume_job(job_id)

    async def _add_to_scheduler(self, schedule: Schedule):
        """Register a single schedule with APScheduler."""
        job_id = self._build_job_id(schedule.id)
        from apscheduler.triggers.cron import CronTrigger
        trigger = CronTrigger.from_crontab(
            schedule.cron_expression,
            timezone=schedule.timezone or "UTC",  # D-07 — was hardcoded "UTC"
        )
        self._scheduler.add_job(
            _execute_scheduled_job,
            trigger=trigger,
            args=[str(schedule.id)],
            id=job_id,
            replace_existing=True,  # Pitfall #3 mitigation
            # The Schedule API allows an empty name (test_create_schedule_empty_name_accepted),
            # but APScheduler's Job requires a nonempty string — fall back to a stable default.
            name=schedule.name or f"Schedule {schedule.id}",
            # APScheduler's default misfire_grace_time is 1 second — a worker restart or
            # brief downtime at the exact fire time silently drops the run instead of
            # catching up. Widen the grace window and coalesce missed fires into one.
            misfire_grace_time=900,
            coalesce=True,
        )

    async def _update_next_run_at(self, schedule: Schedule):
        """Write the next fire time from APScheduler back to the DB (as UTC)."""
        job_id = self._build_job_id(schedule.id)
        job = self._scheduler.get_job(job_id)
        next_run = job.next_run_time if job else None
        if next_run and next_run.tzinfo:
            next_run = next_run.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        async with async_session_maker() as session:
            result = await session.execute(
                select(Schedule).where(Schedule.id == schedule.id)
            )
            db_schedule = result.scalar_one_or_none()
            if db_schedule:
                db_schedule.next_run_at = next_run
                await session.commit()

    async def _update_next_run_at_by_id(self, schedule_id: uuid.UUID):
        """Write the next fire time from APScheduler back to the DB by schedule ID (as UTC).

        _execute_scheduled_job (this method's only caller) can run in the backend API
        process too — POST /schedules/{id}/run fires it directly for an immediate
        "Run Now" — where no live APScheduler instance exists (SchedulerManager.start()
        is only called from the worker's on_startup hook). Without this guard the
        AttributeError below was silently swallowed by the fire-and-forget
        asyncio.ensure_future call, aborting before the ARQ job enqueue and leaving the
        Task stuck at PENDING forever.
        """
        if not self._scheduler:
            return
        job_id = self._build_job_id(schedule_id)
        job = self._scheduler.get_job(job_id)
        next_run = job.next_run_time if job else None
        if next_run and next_run.tzinfo:
            next_run = next_run.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        async with async_session_maker() as session:
            result = await session.execute(
                select(Schedule).where(Schedule.id == schedule_id)
            )
            db_schedule = result.scalar_one_or_none()
            if db_schedule:
                db_schedule.next_run_at = next_run
                await session.commit()

    def _build_job_id(self, schedule_id: uuid.UUID) -> str:
        """Standardized job ID format: schedule_{uuid}"""
        return f"schedule_{schedule_id}"

    async def _persist_next_run_at_batch(self, pairs: dict[uuid.UUID, datetime | None]) -> None:
        """D-01/D-02: persist a batch of (schedule_id -> next_run_at) pairs with a
        single parameterized `UPDATE schedules SET next_run_at = CASE ... END
        WHERE id = ANY(:ids)` in one transaction — constant one-statement DB work
        regardless of how many schedules are supplied.

        D-03: a `None` value writes NULL for that schedule (failed cron
        registration / no scheduler job), exactly like the per-row
        `_update_next_run_at` write it replaces on the load paths.

        Skips the UPDATE entirely when `pairs` is empty (e.g. a refresh pass
        with no new/changed/failed schedules) rather than issuing a no-op
        `WHERE id = ANY('{}')` statement.

        Only `_load_enabled_schedules()` (startup, D-01) and `_refresh_schedules()`
        (periodic refresh, D-01) call this. The per-fire path
        (`_update_next_run_at_by_id`) and the single-schedule REST-triggered call
        sites (`add_job`/`update_job`, via `_update_next_run_at`) are unchanged and
        stay targeted single-row writes — batching there is pure overhead.

        Defined as a `SchedulerManager` method (not a module-level function or
        closure) so the module-level, APScheduler-picklable `_refresh_schedules`
        can call it by reference through the `scheduler_manager` singleton without
        introducing an unpicklable closure.
        """
        if not pairs:
            return
        ids = list(pairs.keys())
        # A CASE expression whose matched branches are ALL literal NULL (e.g. a
        # single failed registration, or an all-failed batch) gives Postgres no
        # non-NULL value to infer a type from — it defaults to `text`/`unknown`
        # and asyncpg then rejects the UPDATE with "column ... is of type
        # timestamp without time zone but expression is of type text". The
        # explicit cast() pins the CASE expression's type to the column's own
        # type regardless of how many branches are NULL.
        stmt = (
            update(Schedule)
            .where(Schedule.id.in_(ids))
            .values(next_run_at=cast(case(
                *[(Schedule.id == sid, value) for sid, value in pairs.items()],
                else_=None,
            ), DateTime))
        )
        async with async_session_maker() as session:
            await session.execute(stmt)
            await session.commit()


# Singleton
scheduler_manager = SchedulerManager()


def _cron_trigger_changed(existing_job, schedule) -> bool:
    """D-01..D-04 (FIX-01): True when the registered job's trigger no longer
    matches the schedule's stored cron_expression/timezone (OR semantics).

    Rebuilds the expected trigger exactly the way _add_to_scheduler does
    (CronTrigger.from_crontab) and compares normalized field strings +
    timezone. Pure function — no scheduler/DB side effects, unit-testable
    with a fake trigger. Raises ValueError for an invalid cron_expression
    (same as _add_to_scheduler) — callers must guard it.
    """
    from apscheduler.triggers.cron import CronTrigger
    existing = existing_job.trigger
    expected = CronTrigger.from_crontab(
        (schedule.cron_expression or "").strip(),      # D-02: strip whitespace only
        timezone=schedule.timezone or "UTC",           # D-03: None -> "UTC" default
    )
    return (
        [str(f) for f in existing.fields] != [str(f) for f in expected.fields]
        # D-03: trigger tz missing -> "" never equals the "UTC" default
        or str(getattr(existing, "timezone", "")) != (schedule.timezone or "UTC")
    )


def _runtime_paused(schedule) -> bool:
    """True when ``schedule_service`` projected this row as runtime-paused (D-01).

    The import is function-local: ``schedule_service`` imports this module at
    import time, so a module-level import here would be circular.
    """
    from app.services.schedule_service import RUNTIME_STATUS_PAUSED

    return getattr(schedule, "runtime_status", None) == RUNTIME_STATUS_PAUSED


def _read_next_run_at(manager: "SchedulerManager", schedule_id: uuid.UUID) -> datetime | None:
    """Read a schedule's computed next fire time from the scheduler's job
    registry, converted to naive UTC — the same value extraction
    `_update_next_run_at`/`_update_next_run_at_by_id` perform inline, shared here
    only for the batched load-path writers (`_load_enabled_schedules`,
    `_refresh_schedules`) so the conversion logic isn't triple-duplicated.
    Returns None if no job is registered under this schedule's job id (D-03:
    failed registration)."""
    job_id = manager._build_job_id(schedule_id)
    job = manager._scheduler.get_job(job_id)
    next_run = job.next_run_time if job else None
    if next_run and next_run.tzinfo:
        next_run = next_run.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return next_run


@with_scheduler_log_context
async def _execute_scheduled_job(schedule_id: str):
    """Called by APScheduler. Loads the Schedule, clones the referenced template
    (a Task class, ExportDefinition, or ImportDefinition) into an instance with
    schedule_id set, and dispatches via task_service.enqueue_task_job.

    D-04: Template cloning — the instance is a full copy, not a reference.
    D-09: Every scheduled run creates a Task record.
    D-11: Manually triggered tasks have schedule_id = null.
    D-16: Failed scheduled runs mark Task as FAILED with error_message.
    """
    from app.services import import_service, export_service
    from app.schemas.crud import ImportCreate, ExportCreate

    task_logger.info("scheduled job fired")
    task_id = None
    task = None
    async with async_session_maker() as session:
        try:
            result = await session.execute(
                select(Schedule).where(Schedule.id == uuid.UUID(schedule_id))
            )
            schedule = result.scalar_one_or_none()
            if not schedule:
                logger.error(f"Schedule {schedule_id} not found")
                return

            template_type = schedule.template_type
            template_id = schedule.template_id
            if not template_type or not template_id:
                logger.error(f"Schedule {schedule_id} has no template configured")
                return

            if template_type == "task":
                # Clone a Task class (schedule_id IS NULL) into an instance
                task_result = await session.execute(
                    select(Task).where(Task.id == template_id)
                )
                template_task = task_result.scalar_one_or_none()
                if not template_task:
                    logger.error(f"Schedule {schedule_id}: Task template {template_id} not found")
                    return

                # Phase 289 (CORESEAM-07, D-01/D-06/D-08/D-09/D-12): a declared
                # plugin task keeps its immutable plugin_parameters verbatim and
                # starts with no business step; a Core task keeps the legacy
                # SCHEMA start step. The clone then passes through the EXISTING
                # central admission gate below — this callback never performs a
                # second worker-version/parameter/state check of its own.
                is_plugin_task = (
                    template_task.tasktype not in task_service.CORE_TASKTYPE_VALUES
                )
                task_data = TaskCreate(
                    schedule_id=uuid.UUID(schedule_id),
                    source_id=template_task.source_id,
                    schema_id=template_task.schema_id,
                    object_type_id=template_task.object_type_id,
                    target_id=template_task.target_id,
                    service_account_id=template_task.service_account_id,
                    tasktype=template_task.tasktype,
                    current_step=(
                        None if is_plugin_task else TaskStep.SCHEMA.value
                    ),
                    current_status=TaskStatus.PENDING.value,
                    plugin_parameters=(
                        template_task.plugin_parameters if is_plugin_task else None
                    ),
                )
                # D-06: the trusted scheduler-origin admission context is minted
                # HERE — after loading the PERSISTED Schedule and its template,
                # inside the Core-owned callback only. No route, TaskCreate body
                # field, schedule_id value or absent actor can produce it. The
                # central admission still re-checks plugin state, the single
                # worker-version gate, parameters and the declaration's admit
                # hook immediately before persistence (now against this
                # process's own registry snapshot — review correction 1).
                task = await task_service.create_task(
                    session,
                    task_data,
                    # D-04: the scheduled clone inherits the PERMANENT template's
                    # creator as its own creator, so a task-bound plugin SDK write
                    # during a scheduled run audits the template's creator — not
                    # the scheduler process and not whoever last edited the
                    # schedule. A legacy template with no creator yields a
                    # creatorless clone (explicit None) whose SDK writes fail
                    # closed. Run-now authorization is unchanged.
                    creator_id=template_task.creator_id,
                    admission_context=task_service.trusted_scheduled_admission_context(
                        uuid.UUID(schedule_id)
                    ),
                )

            elif template_type == "export":
                # Resolve ExportDefinition → ObjectType → schema_id
                def_result = await session.execute(
                    select(ExportDefinition, ObjectType.schema_id)
                    .join(ObjectType, ObjectType.id == ExportDefinition.object_type_id)
                    .where(ExportDefinition.id == template_id)
                )
                row = def_result.one_or_none()
                if not row:
                    logger.error(f"Schedule {schedule_id}: ExportDefinition {template_id} not found")
                    return
                _definition, schema_id = row

                # WR-03: target_id is required at ExportDefinition creation time, but its
                # FK is ondelete="SET NULL", so a previously-valid definition can drift to
                # target_id=None if its Target is deleted later. Catch that here with a
                # clear log entry instead of letting the scheduled run fail deep inside
                # execute_csv_export with a confusing "Target None not found" message.
                if _definition.target_id is None:
                    logger.error(
                        f"Schedule {schedule_id}: ExportDefinition {template_id} has no "
                        f"target configured (its Target may have been deleted)"
                    )
                    return

                # 261022-np1: the schedule's creator is the acting user for a
                # scheduled export. Without one (legacy/system schedule) the run
                # fails closed BEFORE any Task/Export row is written: the export
                # content selection needs a principal, so a creatorless schedule
                # must not leave a FAILED row behind on every fire.
                if schedule.creator_id is None:
                    logger.error(
                        "Schedule %s has no creator; refusing scheduled export",
                        schedule_id,
                    )
                    return

                # Create Export instance owned by the schedule creator, so the
                # file is visible to that user only (exports ownership rule).
                # object_key is server-authored and starts empty.
                export_obj = await export_service.create_export(
                    session,
                    ExportCreate(schema_id=schema_id, status="pending"),
                    creator_id=schedule.creator_id,
                )

                # Create Task linked to Export with export_definition_id. The
                # explicit creator makes the scheduled clone run with the
                # schedule creator's visibility.
                task_data = TaskCreate(
                    schedule_id=uuid.UUID(schedule_id),
                    tasktype=TaskType.EXPORT.value,
                    export_id=export_obj.id,
                    export_definition_id=template_id,
                    current_step=TaskStep.SCHEMA.value,
                    current_status=TaskStatus.PENDING.value,
                )
                task = await task_service.create_task(
                    session, task_data, creator_id=schedule.creator_id
                )

            elif template_type == "import":
                # D-20: Scheduled imports are deferred — no persistent data source for
                # CSV-only imports. schedule_service._resolve_template already rejects
                # template_type='import' at schedule creation/update time, so this branch
                # should be unreachable in normal operation — it stays only as
                # defense-in-depth against a schedule row that predates that validation
                # (or was written directly to the DB).
                logger.error(
                    f"Schedule {schedule_id}: Scheduled imports require a persistent "
                    f"data source — not supported for CSV-only imports"
                )
                return

            else:
                logger.error(f"Schedule {schedule_id} has unknown template_type: {template_type}")
                return

            task_id = str(task.id)
            schedule.last_run_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await session.commit()

        except task_service.PluginTaskAdmissionError as e:
            # 289-07 review correction 4: a job that fires between a refresh and
            # its plugin/worker becoming unavailable is refused by the central
            # admission gate. There is NO Task row to mark FAILED — log the
            # Core-authored bounded reason and return so the scheduler never
            # crashes and no partial bookkeeping is written.
            logger.error(
                "Scheduled job %s refused by plugin admission: %s", schedule_id, e
            )
            return

        except Exception as e:
            logger.error(f"Failed to create Task for scheduled job {schedule_id}: {e}")
            return

    # Refresh next_run_at after execution
    await scheduler_manager._update_next_run_at_by_id(uuid.UUID(schedule_id))

    try:
        pool = await get_arq_pool()
        await task_service.enqueue_task_job(pool, task.tasktype, task_id, task,
            export_definition_id=str(task.export_definition_id) if task.export_definition_id else None,
            import_definition_id=str(task.import_definition_id) if task.import_definition_id else None)

    except Exception as e:
        logger.error(f"Failed to enqueue scheduled job {schedule_id}: {e}")
        # D-16: Mark Task as FAILED
        if task_id:
            async with async_session_maker() as s:
                result = await s.execute(
                    select(Task).where(Task.id == uuid.UUID(task_id))
                )
                t = result.scalar_one_or_none()
                if t:
                    from app.services.task_log_service import append_task_log

                    t.current_status = TaskStatus.FAILED
                    # Carry-03: safe persisted error text, never the raw exception.
                    t.error_message = task_service.safe_error_text(
                        e, "ARQ enqueue failed"
                    )
                    t.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    await append_task_log(
                        s,
                        t.id,
                        logging.WARNING,
                        "ARQ enqueue failed — the scheduled job was never queued",
                    )
                    await s.commit()


@with_scheduler_log_context
async def _run_outbox_sweeper() -> None:
    """APScheduler entry for the outbox sweeper: reset context + trigger=schedule.

    The lazy import avoids the module cycle (app.worker imports this module);
    the registered reference stays picklable because the module-level name is
    preserved by functools.wraps.
    """
    from app.worker import outbox_sweeper_job

    await outbox_sweeper_job({})


@with_scheduler_log_context
async def _refresh_schedules():
    """Periodic sync: reload enabled schedules from DB, add new ones,
    remove disabled/deleted ones, update changed cron expressions.
    Module-level function (not a method) so APScheduler can pickle it."""
    from app.services.schedule_service import (
        RUNTIME_STATUS_PAUSED,
        RUNTIME_STATUS_SCHEDULED,
        schedule_runtime_states,
    )

    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Schedule).where(Schedule.enabled == True)
            )
            db_schedules = {s.id: s for s in result.scalars().all()}
            # Phase 289 (289-07, D-01/D-03): resolve dispatchability for this
            # pass with the same persisted-truth predicate the API projection
            # uses. T-289-07-03: a resolution failure falls back to today's
            # behaviour instead of aborting the refresh.
            try:
                states = await schedule_runtime_states(
                    session, list(db_schedules.values())
                )
            except Exception:
                logger.exception(
                    "Schedule refresh: runtime resolution failed; treating "
                    "every schedule as dispatchable (fail-open)"
                )
                states = {}

        # Collect currently registered schedule job IDs
        registered = {
            uid: job_id
            for job_id, uid in (
                (j.id, j.id.removeprefix("schedule_"))
                for j in scheduler_manager._scheduler.get_jobs()
                if j.id.startswith("schedule_")
            )
        }

        # D-01/D-02: accumulate (schedule_id -> next_run_at) pairs for schedules
        # newly added or cron/timezone-rescheduled THIS refresh pass, and persist
        # them all with one batched UPDATE after the loop instead of one per-row
        # write per schedule.
        #
        # D-03 applies to the STARTUP load path only (_load_enabled_schedules),
        # not here: today, a failed _add_to_scheduler() in either the "add new"
        # or "reschedule" branch below skips the subsequent next_run_at write
        # entirely — no write occurs, the row's pre-existing next_run_at is left
        # untouched. Both branches guard _add_to_scheduler with try/except so a
        # single bad schedule can't raise out of the loop and discard the
        # already-accumulated pairs for schedules processed earlier in this pass
        # (CR-01) — that asymmetry is preserved unchanged; only successful
        # registration attempts (new or rescheduled) contribute a pair to the batch.
        pairs: dict[uuid.UUID, datetime | None] = {}

        # Add new or updated schedules
        for sid, schedule in db_schedules.items():
            job_id = scheduler_manager._build_job_id(sid)
            existing_job = scheduler_manager._scheduler.get_job(job_id)

            if (
                states.get(sid, (RUNTIME_STATUS_SCHEDULED, None))[0]
                == RUNTIME_STATUS_PAUSED
            ):
                # D-01/D-03: runtime pause = remove/leave absent ONLY the job.
                # enabled stays the user's decision; a stale future next_run_at
                # is cleared so the pause never advertises or replays a run.
                # NOTE: never pause_job() — the registered jobs carry
                # misfire_grace_time=900/coalesce=True, so a resume would
                # replay a missed occurrence.
                if existing_job is not None:
                    scheduler_manager._scheduler.remove_job(job_id)
                    pairs[sid] = None
                    logger.info("Schedule refresh: runtime-paused %s", sid)
                elif schedule.next_run_at is not None:
                    pairs[sid] = None
                continue

            if existing_job is None:
                try:
                    await scheduler_manager._add_to_scheduler(schedule)
                    pairs[sid] = _read_next_run_at(scheduler_manager, sid)
                    logger.info("Schedule refresh: added %s", sid)
                except Exception as e:
                    logger.error("Schedule refresh: failed to add %s: %s", sid, e)
                    # No write on failure (today's behavior, unchanged) — the
                    # row's pre-existing next_run_at stays as-is.
            else:
                # Check if cron or timezone changed — reschedule if so. The
                # try/except encloses the helper call too: _cron_trigger_changed
                # raises ValueError on an invalid stored cron_expression (same
                # construction as _add_to_scheduler), and that failure must not
                # discard already-accumulated pairs for other schedules
                # processed earlier in this pass (CR-01).
                try:
                    if _cron_trigger_changed(existing_job, schedule):
                        await scheduler_manager._add_to_scheduler(schedule)  # replace_existing=True
                        pairs[sid] = _read_next_run_at(scheduler_manager, sid)
                        logger.info("Schedule refresh: updated %s", sid)
                except Exception as e:
                    logger.error("Schedule refresh: failed to reschedule %s: %s", sid, e)
                    # No write on failure, consistent with the "add new" branch
                    # above — the row's pre-existing next_run_at stays as-is, and
                    # this failure must not discard already-accumulated pairs for
                    # other schedules processed earlier in this pass (CR-01).

        await scheduler_manager._persist_next_run_at_batch(pairs)

        # Remove jobs for schedules that are disabled or deleted
        for sid, job_id in registered.items():
            # sid is a string (from job ID), db_schedules keys are UUID — compare as strings
            if uuid.UUID(sid) not in db_schedules:
                scheduler_manager._scheduler.remove_job(job_id)
                task_logger.info("schedule refresh: removed %s", sid)

    except Exception:
        task_logger.exception("schedule refresh failed")


@with_scheduler_log_context
async def _run_task_log_purge() -> None:
    """APScheduler entry for task-log retention (plan 300-03, D-11/D-12).

    No task context is bound: one pass purges every finished task's expired
    lines. The retention window is read from the environment each run, so a
    changed ``TASK_LOG_RETENTION_DAYS`` takes effect without a restart.
    """
    from app.logging.task_log import TaskLogConfig
    from app.services.task_log_service import purge_task_logs

    config = TaskLogConfig.from_env()
    await purge_task_logs(
        async_session_maker, retention_days=config.retention_days
    )
