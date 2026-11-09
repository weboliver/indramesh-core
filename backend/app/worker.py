import asyncio
import contextlib
import json
import logging
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps

from arq.connections import RedisSettings
from sqlalchemy import select, update

from app.crypto import init_fernet
from app.logging import configure_logging
from app.logging.context import bind_log_context, reset_log_context
from app.logging.discovery import LoggingCategoryDiscovery
from app.logging.levels import LevelController
from app.logging.task_log import (
    TaskLogConfig,
    TaskLogWriter,
    clear_task_log_writer,
    set_task_log_writer,
)
from app.logging.worker import bind_job_context, with_log_context
from app.db import async_session_maker, log_identity_split_warning
from app.db.opensearch import init_opensearch, close_opensearch
from app.db.redis import get_arq_pool, init_redis, close_redis
from app.services.scheduler_manager import scheduler_manager, init_job_registry, _execute_scheduled_job
from app.plugin_system import get_plugin_registry, init_plugin_registry, task_registry
from app.plugin_system.capabilities import PluginTaskError
from app.plugin_system.registry import STATE_CACHE_TTL, PluginCallError
from app.models.import_export import Import, ImportStatusEnum, Export, ExportStatusEnum
from app.models.schema import Schema
from app.models.task import Task, TaskStep, TaskStatus
from app.schemas.crud import TaskUpdate, ImportUpdate, ExportUpdate
from app.services import task_service, import_service, export_service
from app.services import hook_origin
from app.services.task_log_service import append_task_log
from app.services.search_service import (
    index_object as index_object_svc,
    remove_object_from_index,
    bulk_reindex as bulk_reindex_svc,
    index_object_type,
    remove_object_type_from_index,
    remove_object_type_documents,
    delete_index,
    index_schema,
    remove_schema_from_index,
)
from app.models.index_outbox import IndexOutbox, IndexOutboxStatus
from app.services.cache_service import invalidate_entity_cache
from app.services.audit_service import with_audit_context

# app.worker -> category "app" (infra/outbox lines); task.worker -> category
# "task" (job lifecycle and task mutations, D-16).
logger = logging.getLogger(__name__)
task_logger = logging.getLogger("task.worker")

# Phase 312.1 (D-15/D-16): operator-visible name renamed; scheme and library names stay.
VALKEY_URL = os.getenv("VALKEY_URL", "redis://valkey:6379/0")

# ARQ job timeout in seconds. Configurable because long-running plugin tasks
# (e.g. Jira sync work dispatched through the generic run_plugin_task wrapper),
# rate-limited one entity at a time against the real Jira REST API, can
# legitimately run far longer than a typical background job. NOTE: arq's
# Worker.__init__ computes
# max(f.timeout_s or self.job_timeout_s for f in self.functions.values()) —
# passing a real `None` here makes every term None and that max() raises
# TypeError (`'>' not supported between NoneType and NoneType`) before the
# worker even starts. So "0 = unlimited" maps to a very large finite value
# (24h) instead of None.
_JOB_TIMEOUT_RAW = os.getenv("ARQ_JOB_TIMEOUT_SECONDS", "1800")
_UNLIMITED_JOB_TIMEOUT = 86400
try:
    JOB_TIMEOUT: int = int(_JOB_TIMEOUT_RAW) or _UNLIMITED_JOB_TIMEOUT
except ValueError:
    JOB_TIMEOUT = 1800


def _parse_redis_settings() -> RedisSettings:
    rest = VALKEY_URL[8:] if VALKEY_URL.startswith("redis://") else VALKEY_URL
    user_info = None
    host_part = rest
    if "@" in rest:
        user_info, host_part = rest.split("@", 1)
    host = "localhost"
    port = 6379
    db = 0
    password = None
    username = None
    if ":" in host_part and host_part.count(":") == 1:
        host, rest_port = host_part.split(":", 1)
        if "/" in rest_port:
            port_str, db_str = rest_port.split("/", 1)
            port = int(port_str)
            db = int(db_str) if db_str else 0
        else:
            port = int(rest_port)
    elif "/" in host_part:
        host, rest_path = host_part.split("/", 1)
        if rest_path:
            try:
                db = int(rest_path)
            except ValueError:
                pass
    else:
        host = host_part if host_part else "localhost"
    if user_info:
        if ":" in user_info:
            username, password = user_info.split(":", 1)
        else:
            username = user_info
    return RedisSettings(
        host=host, port=port, database=db,
        username=username, password=password if password else None,
    )


_redis_settings = _parse_redis_settings()

# Per D-13: Which entity types each job function can affect.
# Used by invalidate_after decorator to trigger post-execution cache invalidation.
_JOB_ENTITY_MAP: dict[str, list[str]] = {
    "process_import":  ["object", "object_value", "object_ref", "task"],
    "process_export":  ["export", "export_status", "task"],
    "process_folder_import": ["object", "object_value", "object_ref", "task"],
}


def invalidate_after(job_name: str):
    """Decorator that invalidates Redis cache for entity types touched by a worker job.

    After the decorated job function completes successfully (returns a dict
    with "success": True or "success" not set to False), all entity types
    listed in _JOB_ENTITY_MAP for this job_name are invalidated via
    invalidate_entity_cache(). This closes the stale-data window that would
    otherwise persist until TTL expiry.

    Usage:
        @invalidate_after("process_import")
        async def process_import(ctx, task_id):
            ...
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            result = await func(*args, **kwargs)
            # Only invalidate on successful completion (not errors)
            if isinstance(result, dict) and result.get("success") is not False:
                entity_types = _JOB_ENTITY_MAP.get(job_name, [])
                for entity_type in entity_types:
                    await invalidate_entity_cache(entity_type)
            return result
        return wrapper
    return decorator


# Phase 300 (D-05/D-06): a user cancel is a request. The cancel monitor polls
# ``tasks.cancel_requested_at`` with its own short session and cancels the job
# coroutine; the wrapper then records FAILED "Cancelled by user" after the run's
# cleanup handlers have finished. One worker process, so a module-level set is
# enough to tell the cancellation cleanup handlers apart from an ARQ timeout.
CANCEL_MONITOR_INTERVAL_SECONDS = 2.0
_USER_CANCELLED_TASKS: set[uuid.UUID] = set()

ORPHANED_INDEX_SWEEP_TIMEOUT_SECONDS = 60
# Fixed diagnostics: no index name, id or exception text reaches the log.
ORPHANED_INDEX_SWEEP_FAILED = "Orphaned search index sweep failed; continuing the worker start."
ORPHANED_INDEX_SWEEP_DELETED = "Deleted %d orphaned search index(es)."


def is_user_cancel_requested(task_id) -> bool:
    """True while the cancel monitor has stopped this task (D-06)."""
    try:
        return uuid.UUID(str(task_id)) in _USER_CANCELLED_TASKS
    except (ValueError, TypeError, AttributeError):
        return False


async def _cancel_requested(task_uuid: uuid.UUID, session_factory) -> bool:
    async with session_factory() as session:
        value = await session.scalar(
            select(Task.cancel_requested_at).where(Task.id == task_uuid)
        )
        return value is not None


async def _finalize_user_cancel(task_uuid: uuid.UUID, session_factory) -> None:
    """Write the terminal FAILED after a user-cancelled run has cleaned up (D-06)."""
    try:
        async with session_factory() as session:
            task = await task_service.get_task(session, task_uuid)
            if task is None:
                return
            if task.current_status in task_service.TERMINAL_TASK_STATUSES:
                return
            await task_service.update_task(
                session,
                task_uuid,
                TaskUpdate(
                    current_status=TaskStatus.FAILED.value,
                    error_message=task_service.USER_CANCEL_MESSAGE,
                    completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                ),
            )
    except Exception:
        logger.exception("failed to finalize user cancel for task %s", task_uuid)


def with_cancel_monitor(fn, *, interval: float | None = None, session_factory=None):
    """Run ``fn`` beside a poller that turns a cancel request into a stop (D-05).

    The poller reads ``tasks.cancel_requested_at`` with its own short session.
    Once set, it marks the task id in ``_USER_CANCELLED_TASKS`` and cancels the
    job coroutine. After the job's cleanup handlers finish, the wrapper writes
    the terminal FAILED "Cancelled by user" (through ``update_task``, so the
    D-07 flush barrier applies) and swallows the CancelledError so ARQ never
    retries a user cancel. A plain ARQ timeout/shutdown (no flag) propagates
    unchanged. ``interval`` and ``session_factory`` are seams for fast tests.
    """

    @wraps(fn)
    async def wrapper(ctx, task_id, *args, **kwargs):
        try:
            task_uuid = uuid.UUID(str(task_id))
        except (ValueError, TypeError, AttributeError):
            return await fn(ctx, task_id, *args, **kwargs)

        poll_interval = (
            CANCEL_MONITOR_INTERVAL_SECONDS if interval is None else float(interval)
        )
        factory = session_factory or async_session_maker
        user_cancel = False
        job_task = asyncio.ensure_future(fn(ctx, task_id, *args, **kwargs))

        async def monitor():
            nonlocal user_cancel
            while not job_task.done():
                await asyncio.sleep(poll_interval)
                try:
                    requested = await _cancel_requested(task_uuid, factory)
                except Exception:
                    requested = False
                if requested:
                    user_cancel = True
                    _USER_CANCELLED_TASKS.add(task_uuid)
                    job_task.cancel()
                    return

        monitor_task = asyncio.ensure_future(monitor())
        try:
            return await job_task
        except asyncio.CancelledError:
            if user_cancel:
                await _finalize_user_cancel(task_uuid, factory)
                return {
                    "success": False,
                    "task_id": str(task_id),
                    "error": task_service.USER_CANCEL_MESSAGE,
                    "cancelled": True,
                }
            raise
        finally:
            monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await monitor_task
            _USER_CANCELLED_TASKS.discard(task_uuid)

    return wrapper


async def recover_stuck_tasks() -> int:
    """Fail stale RUNNING tasks and write a recovery line with each status.

    This is the recovery pass ``startup_recovery`` runs once at worker start
    (5-minute rule unchanged). Extracted so it can be tested without the full
    worker startup (Elasticsearch init, plugin registry, scheduler). Each
    FAILED write and its WARNING "Worker restarted …" log line commit in the
    same transaction.
    """
    session = async_session_maker()
    recovered_count = 0
    try:
        result = await session.execute(
            select(Task).where(Task.current_status == TaskStatus.RUNNING)
        )
        stuck_tasks = result.scalars().all()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        recovery_cutoff = timedelta(minutes=5)
        for task in stuck_tasks:
            if task.started_at and (now - task.started_at) > recovery_cutoff:
                task.current_status = TaskStatus.FAILED
                task.error_message = f"Worker restarted — job interrupted (task_id={task.id})"
                task.completed_at = now
                await append_task_log(
                    session,
                    task.id,
                    logging.WARNING,
                    "Worker restarted — run interrupted; log may be incomplete",
                )
                recovered_count += 1
            elif not task.started_at:
                task.current_status = TaskStatus.FAILED
                task.error_message = f"Worker restarted — job never started (task_id={task.id})"
                task.completed_at = now
                await append_task_log(
                    session,
                    task.id,
                    logging.WARNING,
                    "Worker restarted — run interrupted; log may be incomplete",
                )
                recovered_count += 1
        await session.commit()
    except Exception:
        task_logger.exception("worker startup recovery error")
        await session.rollback()
    finally:
        await session.close()
    return recovered_count


async def _reconcile_plugin_states() -> None:
    # A plugin enabled or disabled through the backend API only reaches this
    # process via is_active(); without a periodic pass, a plugin that was
    # DISABLED at worker start would never activate here until a restart.
    registry = get_plugin_registry()
    while True:
        await asyncio.sleep(STATE_CACHE_TTL)
        await registry.reconcile_all()


async def sweep_orphaned_search_indices() -> int:
    """Delete search indices without a schema; never raises, never fails the start.

    Indices deleted before a timeout stay deleted, and the next start continues.
    """
    from app.services.search_service import delete_orphaned_schema_indices

    try:
        async with async_session_maker() as session:
            deleted = await asyncio.wait_for(
                delete_orphaned_schema_indices(session),
                timeout=ORPHANED_INDEX_SWEEP_TIMEOUT_SECONDS,
            )
    except Exception:
        logger.warning(ORPHANED_INDEX_SWEEP_FAILED)
        return 0
    logger.info(ORPHANED_INDEX_SWEEP_DELETED, deleted)
    return deleted


async def startup_recovery(ctx: dict) -> None:
    # Re-apply the shared configuration idempotently: arq's CLI applies its own
    # log dict after importing WorkerSettings, and a deployment that forgets
    # --custom-log-dict would otherwise leave arq's plain-text handler attached
    # (research Pitfall 3). Nothing is bound here — APScheduler inherits the
    # context active at scheduler.start() (research Pitfall 7).
    configure_logging("worker")

    # D-08 / Pitfall 10: an explicit DATABASE_URL silently disables the
    # runtime/maintenance identity split. Make the weakened state visible at
    # worker startup; never abort — the operator's URL is an allowed config.
    log_identity_split_warning(logger)

    import asyncio

    # Phase 300: start the process-wide task-log writer right after the stdout
    # logging is in place, so worker lifecycle lines already flow into task_logs.
    # The handler is installed exactly once and configure_logging never removes a
    # foreign handler. Invalid TASK_LOG_MAX_LINES/RETENTION_DAYS fail startup.
    task_log_writer = TaskLogWriter(
        async_session_maker, config=TaskLogConfig.from_env()
    )
    task_log_writer.install_handler()
    await task_log_writer.start()
    set_task_log_writer(task_log_writer)
    ctx["task_log_writer"] = task_log_writer

    # Worker runs as a separate process from the FastAPI app (app/main.py), which
    # initializes the global Fernet singleton in its own lifespan. Without this call,
    # any job touching an EncryptedString column (e.g. ServiceAccount bearer tokens
    # during process_import) raises "Fernet not initialized".
    init_fernet()

    # Same issue as Fernet above — app/db/redis.py's cache-layer Redis client
    # (get_redis(), used by app/services/cache_service.py) is separate from
    # get_arq_pool()'s own connection and is normally initialized by main.py's
    # lifespan. Without this, any worker-driven cache invalidation after an
    # import mutation silently no-ops with "Redis not initialized",
    # leaving stale cached reads until TTL expiry.
    await init_redis()

    # Phase 299 (D-20): the worker is a separate OS process, so it needs its own
    # runtime level controller AND discovery reporter — otherwise a category
    # that only exists in the worker is never offered centrally and a runtime
    # PUT would not reach worker-side DEBUG output. Started before the plugin
    # registry so the controller reads the current DB truth early; the report is
    # refreshed again after plugin import (below).
    level_discovery = LoggingCategoryDiscovery(
        session_factory=async_session_maker, role="worker"
    )
    level_controller = LevelController(
        session_factory=async_session_maker, discovery=level_discovery
    )
    ctx["logging_level_controller"] = level_controller
    ctx["logging_discovery"] = level_discovery
    await level_controller.start()

    try:
        es_task = asyncio.create_task(init_opensearch())
        done, _ = await asyncio.wait([es_task], timeout=105)
        if es_task in done:
            logger.info("worker: OpenSearch client initialized")
        else:
            logger.warning("worker: OpenSearch init timed out — will retry on job execution")
    except Exception as e:
        logger.warning("worker: OpenSearch init deferred (%s)", e)

    recovered_count = await recover_stuck_tasks()
    if recovered_count > 0:
        task_logger.info(
            "worker startup: recovered %d stuck task(s) — marked FAILED",
            recovered_count,
        )

    await sweep_orphaned_search_indices()

    # Initialize APScheduler and load enabled schedules
    init_job_registry()

    # PLUGCORE-10 / D-13: same dual-process wiring as main.py's lifespan — the
    # worker must build its OWN PluginRegistry from the core `plugins` table or
    # a kill-switch issued via the backend API would never reach worker-side
    # capability/hook calls. Runs after Fernet/Redis/ES init above.
    # PLUGWORKER-02/D-01: is_worker=True — this is the ONLY production site that
    # flips the role flag, making the worker the sole writer of the
    # worker_loaded_at/worker_loaded_version snapshot.
    async with async_session_maker() as plugin_session:
        await init_plugin_registry(plugin_session, is_worker=True)
    ctx["plugin_reconcile_task"] = asyncio.create_task(_reconcile_plugin_states())

    # Plugin import created new worker-side loggers; re-apply the active policy
    # and refresh the discovery report so plugin-only categories are offered.
    level_controller.policy.apply_all()
    await level_discovery.heartbeat(force=True)

    try:
        task_logger.info("worker: starting APScheduler")
        await scheduler_manager.start()
        task_logger.info("worker: APScheduler initialized with SQLAlchemyJobStore")
    except ModuleNotFoundError:
        task_logger.warning("worker: APScheduler not available — skipping scheduler startup")
    except Exception:
        task_logger.exception("worker: APScheduler startup failed")


async def shutdown(ctx: dict) -> None:
    # Phase 299 (D-20): stop the level controller (cancels its poll task) and
    # remove this worker's discovery lease BEFORE closing Redis.
    controller = ctx.get("logging_level_controller") if ctx else None
    if controller is not None:
        await controller.stop()
    reconcile_task = ctx.get("plugin_reconcile_task") if ctx else None
    if reconcile_task is not None:
        reconcile_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reconcile_task
    await scheduler_manager.shutdown()
    task_logger.info("worker: APScheduler shut down")
    # Phase 300: stop the task-log writer before the DB/Redis resources it uses
    # are closed. stop() flushes every buffer with retry and removes the handler.
    task_log_writer = ctx.get("task_log_writer") if ctx else None
    if task_log_writer is not None:
        await task_log_writer.stop()
        clear_task_log_writer(task_log_writer)
    await close_opensearch()
    await close_redis()


@with_audit_context
@invalidate_after("process_import")
async def process_import(ctx: dict, task_id: str, import_definition_id: str | None = None, temp_file_path: str | None = None) -> dict:
    session = async_session_maker()
    try:
        task_uuid = uuid.UUID(task_id)

        # Set import_definition_id unconditionally up front (not only on the SUCCESS
        # path below) — ImportListPage's "Recent Import Runs" filters Task rows by
        # import_definition_id, so a FAILED run left it null and silently vanished
        # from the definition's history instead of surfacing the failure (same class
        # of bug fixed for process_export's export_definition_id).
        if not await task_service.start_task_run(session, task_uuid):
            return {"success": False, "error": "Task not running"}
        if import_definition_id:
            await task_service.update_task(
                session,
                task_uuid,
                TaskUpdate(import_definition_id=uuid.UUID(import_definition_id)),
            )
        task = await task_service.get_task(session, task_uuid)

        result = await session.execute(
            select(Import).where(Import.id == task.import_id)
        )
        import_obj = result.scalar_one_or_none()
        if not import_obj:
            task_update = TaskUpdate(
                current_status=TaskStatus.FAILED.value,
                error_message="Import record not found",
                completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            await task_service.update_task(session, task_uuid, task_update)
            return {"success": False, "error": "Import not found"}

        result = await session.execute(
            select(Schema).where(Schema.id == import_obj.schema_id)
        )
        schema = result.scalar_one_or_none()
        if not schema:
            task_update = TaskUpdate(
                current_status=TaskStatus.FAILED.value,
                error_message="Schema not found for import",
                completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            await task_service.update_task(session, task_uuid, task_update)
            return {"success": False, "error": "Schema not found"}

        import_update = ImportUpdate(status=ImportStatusEnum.PROCESSING.value)
        await import_service.update_import(session, import_obj.id, import_update)

        if import_definition_id and temp_file_path:
            # CSV import mode — delegate to import_execution_service
            from app.services.import_execution_service import execute_csv_import
            try:
                import_def_uuid = uuid.UUID(import_definition_id)

                csv_result = await execute_csv_import(
                    db=session,
                    import_definition_id=import_def_uuid,
                    import_id=import_obj.id,
                    temp_file_path=temp_file_path,
                    actor_id=ctx.get("system_user_id"),
                    task_id=task_uuid,
                )

                persist_kwargs = {}
                if import_definition_id:
                    persist_kwargs["import_definition_id"] = uuid.UUID(import_definition_id)
                # WR-01: derive the task status from the actual import outcome —
                # execute_csv_import never raises for a syntactically valid but
                # semantically dead CSV (wrong column headers, all rows failing
                # validation); it returns processed_count=0 with error_count>0 and
                # marks the Import row "failed". Mirror that outcome on the Task so
                # the Tasks UI never shows SUCCESS for a fully failed import.
                if csv_result.get("processed_count", 0) > 0 or csv_result.get("error_count", 0) == 0:
                    task_update = TaskUpdate(
                        current_status=TaskStatus.SUCCESS.value,
                        current_step=TaskStep.POSTPROCESSING.value,
                        completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                        **persist_kwargs,
                    )
                else:
                    task_update = TaskUpdate(
                        current_status=TaskStatus.FAILED.value,
                        error_message=(
                            f"Import completed with 0 rows processed "
                            f"({csv_result.get('error_count', 0)} errors)"
                        ),
                        completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    )
                await task_service.update_task(session, task_uuid, task_update)

                return {
                    "success": True,
                    "task_id": task_id,
                    "row_count": csv_result.get("row_count", 0),
                    "processed_count": csv_result.get("processed_count", 0),
                    "error_count": csv_result.get("error_count", 0),
                }
            except Exception as e:
                error_msg = task_service.safe_error_text(e, "CSV import failed")
                task_logger.error("CSV import error: %s", error_msg)
                # WR-07: truncate before assigning to max_length=2000 EscapedStr/PlainStr
                # fields — EscapedStr's BeforeValidator HTML-escapes (up to ~6x expansion
                # for quote-heavy text) before max_length is checked, so an untruncated
                # long exception message (e.g. a DB IntegrityError detail string) can raise
                # a secondary pydantic.ValidationError here, masking the real error.
                truncated_error_msg = error_msg[:1900] if error_msg else error_msg
                task_update = TaskUpdate(
                    current_status=TaskStatus.FAILED.value,
                    error_message=truncated_error_msg,
                    completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                )
                await task_service.update_task(session, task_uuid, task_update)
                import_update = ImportUpdate(
                    status="failed",
                    error_message=truncated_error_msg,
                )
                await import_service.update_import(session, import_obj.id, import_update)
                return {"success": False, "error": error_msg}
        else:
            # A job without a definition-backed CSV payload (import_definition_id +
            # temp_file_path) cannot be served by the Core import pipeline — fail it
            # explicitly instead of silently reporting success. Jira imports are
            # plugin tasks (run_plugin_task), never a Core process_import fallback.
            error_msg = (
                "process_import requires import_definition_id and temp_file_path; "
                "a definition-backed import must be created through "
                "POST /imports/csv/upload or an ImportDefinition-backed task"
            )
            import_update = ImportUpdate(
                status=ImportStatusEnum.FAILED.value,
                error_message=error_msg,
            )
            await import_service.update_import(session, import_obj.id, import_update)
            raise ValueError(error_msg)

    except Exception as e:
        error_msg = task_service.safe_error_text(e, "Import job failed")
        task_logger.error("Import job error: %s", error_msg)
        try:
            # A DB-level exception (e.g. IntegrityError) leaves this session unusable
            # until rolled back — without this, the write below silently fails with
            # PendingRollbackError, swallowed by the bare except, leaving the task
            # stuck at RUNNING with no error_message.
            await session.rollback()
            task_uuid = uuid.UUID(task_id)
            await task_service.mark_task_failed_or_dead_letter(session, task_uuid, error_msg, ctx)
        except Exception:
            pass
        raise
    finally:
        await session.close()


@with_audit_context
@invalidate_after("process_export")
async def process_export(ctx: dict, task_id: str, export_definition_id: str | None = None) -> dict:
    session = async_session_maker()
    try:
        task_uuid = uuid.UUID(task_id)

        # Set export_definition_id unconditionally up front (not only on the SUCCESS
        # path below) — ExportListPage's "Recent Export Runs" filters Task rows by
        # export_definition_id, so a FAILED run left it null and silently vanished
        # from the definition's history instead of surfacing the failure.
        if not await task_service.start_task_run(session, task_uuid):
            return {"success": False, "error": "Task not running"}
        if export_definition_id:
            await task_service.update_task(
                session,
                task_uuid,
                TaskUpdate(export_definition_id=uuid.UUID(export_definition_id)),
            )
        task = await task_service.get_task(session, task_uuid)

        result = await session.execute(
            select(Export).where(Export.id == task.export_id)
        )
        export_obj = result.scalar_one_or_none()
        if not export_obj:
            task_update = TaskUpdate(
                current_status=TaskStatus.FAILED.value,
                error_message="Export record not found",
                completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            await task_service.update_task(session, task_uuid, task_update)
            return {"success": False, "error": "Export not found"}

        # EXT-10-T1: the route answers 400 for a definition-less body, but a task
        # queued before the deploy (or a direct enqueue_job) can still get here.
        # Refuse explicitly — the deleted step-walk stub used to report
        # SUCCESS/COMPLETED while never writing a file. object_key is left alone
        # so nobody can download a path that does not exist.
        if not export_definition_id:
            error_msg = "Export definition required"
            task_logger.error("Export task %s has no export definition", task_id)
            await task_service.update_task(
                session,
                task_uuid,
                TaskUpdate(
                    current_status=TaskStatus.FAILED.value,
                    error_message=error_msg,
                    completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                ),
            )
            await export_service.update_export(
                session,
                export_obj.id,
                ExportUpdate(
                    status=ExportStatusEnum.FAILED.value,
                    error_message=error_msg,
                ),
            )
            return {"success": False, "error": error_msg}

        export_update = ExportUpdate(status=ExportStatusEnum.PROCESSING.value)
        await export_service.update_export(session, export_obj.id, export_update)

        # CSV export mode — delegate to export_execution_service which queries
        # all data, bundles context, and invokes the pure CsvExportConnector.
        from app.services.export_execution_service import execute_csv_export
        definition_uuid = uuid.UUID(export_definition_id)
        try:
            csv_result = await execute_csv_export(
                db=session,
                export_definition_id=definition_uuid,
                export_id=export_obj.id,
                task_id=task_uuid,
            )

            skipped_count = len(csv_result.columns_skipped)
            task_update = TaskUpdate(
                current_status=TaskStatus.SUCCESS.value,
                current_step=TaskStep.POSTPROCESSING.value,
                completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                export_definition_id=definition_uuid,
            )
            await task_service.update_task(session, task_uuid, task_update)

            export_update = ExportUpdate(
                status=ExportStatusEnum.COMPLETED.value,
                total_objects=csv_result.row_count,
                processed_objects=csv_result.row_count,
            )
            # object_key is system-authored: only this worker (and the CSV
            # executor) may persist the file path, never a request.
            await export_service.update_export(
                session, export_obj.id, export_update,
                object_key=csv_result.file_path,
            )

            return {
                "success": True,
                "task_id": task_id,
                "row_count": csv_result.row_count,
                "columns": csv_result.columns,
                "columns_skipped": skipped_count,
                "file_path": csv_result.file_path,
            }
        except Exception as e:
            error_msg = task_service.safe_error_text(e, "CSV export failed")
            task_logger.error("CSV export error: %s", error_msg)
            task_update = TaskUpdate(
                current_status=TaskStatus.FAILED.value,
                error_message=error_msg,
                completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            await task_service.update_task(session, task_uuid, task_update)
            export_update = ExportUpdate(
                status=ExportStatusEnum.FAILED.value,
                error_message=error_msg,
            )
            await export_service.update_export(session, export_obj.id, export_update)
            return {"success": False, "error": error_msg}

    except Exception as e:
        error_msg = task_service.safe_error_text(e, "Export job failed")
        task_logger.error("Export job error: %s", error_msg)
        try:
            await session.rollback()
            task_uuid = uuid.UUID(task_id)
            await task_service.mark_task_failed_or_dead_letter(session, task_uuid, error_msg, ctx)
        except Exception:
            pass
        raise
    finally:
        await session.close()


@with_audit_context
@invalidate_after("process_folder_import")
async def process_folder_import(ctx: dict, task_id: str, import_definition_id: str, source_file_path: str) -> dict:
    """Folder-based counterpart to process_import's CSV branch.

    execute_csv_import() always unlinks the temp_file_path it's given (WR-01) — so
    rather than touch that contract, copy source_file_path to a throwaway temp file
    and hand execute_csv_import the COPY. The original stays untouched until we know
    the outcome: on success it moves to <folder>/processed/, on failure it's left in
    place so the next folder scan can retry it.
    """
    session = async_session_maker()
    temp_copy_path = None
    try:
        task_uuid = uuid.UUID(task_id)
        import_def_uuid = uuid.UUID(import_definition_id)

        if not await task_service.start_task_run(session, task_uuid):
            return {"success": False, "error": "Task not running"}
        await task_service.update_task(
            session, task_uuid, TaskUpdate(import_definition_id=import_def_uuid)
        )
        task = await task_service.get_task(session, task_uuid)

        result = await session.execute(
            select(Import).where(Import.id == task.import_id)
        )
        import_obj = result.scalar_one_or_none()
        if not import_obj:
            task_update = TaskUpdate(
                current_status=TaskStatus.FAILED.value,
                error_message="Import record not found",
                completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            await task_service.update_task(session, task_uuid, task_update)
            return {"success": False, "error": "Import not found"}

        import_update = ImportUpdate(status=ImportStatusEnum.PROCESSING.value)
        await import_service.update_import(session, import_obj.id, import_update)

        from app.services.import_execution_service import execute_csv_import

        fd, temp_copy_path = tempfile.mkstemp(suffix=".csv", prefix="folder_import_")
        os.close(fd)
        shutil.copyfile(source_file_path, temp_copy_path)

        csv_result = await execute_csv_import(
            db=session,
            import_definition_id=import_def_uuid,
            import_id=import_obj.id,
            temp_file_path=temp_copy_path,
            actor_id=ctx.get("system_user_id"),
            task_id=task_uuid,
        )
        temp_copy_path = None  # execute_csv_import already unlinked it (WR-01)

        processed_dir = os.path.join(os.path.dirname(source_file_path), "processed")
        os.makedirs(processed_dir, exist_ok=True)
        shutil.move(source_file_path, os.path.join(processed_dir, os.path.basename(source_file_path)))

        # WR-01: same outcome-derived task status as the single-file import path —
        # a folder import that parsed but processed 0 rows (wrong headers, all
        # rows failing validation) must not surface as a SUCCESS task.
        if csv_result.get("processed_count", 0) > 0 or csv_result.get("error_count", 0) == 0:
            task_update = TaskUpdate(
                current_status=TaskStatus.SUCCESS.value,
                current_step=TaskStep.POSTPROCESSING.value,
                completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        else:
            task_update = TaskUpdate(
                current_status=TaskStatus.FAILED.value,
                error_message=(
                    f"Import completed with 0 rows processed "
                    f"({csv_result.get('error_count', 0)} errors)"
                ),
                completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        await task_service.update_task(session, task_uuid, task_update)

        return {
            "success": True,
            "task_id": task_id,
            "row_count": csv_result.get("row_count", 0),
            "processed_count": csv_result.get("processed_count", 0),
            "error_count": csv_result.get("error_count", 0),
        }
    except Exception as e:
        error_msg = task_service.safe_error_text(e, "Folder import failed")
        task_logger.error("Folder import error: %s", error_msg)
        truncated_error_msg = error_msg[:1900] if error_msg else error_msg
        try:
            await session.rollback()
            task_uuid = uuid.UUID(task_id)
            await task_service.mark_task_failed_or_dead_letter(session, task_uuid, truncated_error_msg, ctx)
        except Exception:
            pass
        if temp_copy_path:
            try:
                os.unlink(temp_copy_path)
            except OSError:
                pass
        # Original file at source_file_path is deliberately left in place — the next
        # folder scan will pick it up again.
        return {"success": False, "error": error_msg}
    finally:
        await session.close()


async def _mark_outbox(outbox_id: str | None, status: str, error: str | None = None) -> None:
    """Best-effort outbox status update — failures here just mean the sweeper
    re-checks this entry later, never a lost index update."""
    if not outbox_id:
        return
    try:
        async with async_session_maker() as session:
            values = {"status": status, "processed_at": datetime.now(timezone.utc).replace(tzinfo=None)}
            if error is not None:
                values["last_error"] = error
            await session.execute(
                update(IndexOutbox).where(IndexOutbox.id == uuid.UUID(outbox_id)).values(**values)
            )
            await session.commit()
    except Exception as e:
        logger.error("failed to update outbox entry %s: %s", outbox_id, e)


async def _load_outbox_attribution(
    outbox_id: str | None,
) -> tuple[uuid.UUID | None, hook_origin.MutationOrigin]:
    """Resolve the persisted mutation origin for one index job (CORESEAM-11/D-11).

    The outbox row — not the ARQ payload and not the worker user — is the source
    of truth, so a sweeper re-enqueue after a Redis outage or a crashed worker
    replays the SAME attribution as the first delivery. An absent/unparseable outbox id,
    a missing row or a legacy all-null row normalizes to the explicit
    system form (``core-index``, null actor/task, D-13); a malformed persisted
    kind also falls back to system rather than claiming plugin authority
    (T-291.2-10). A failed DB read is NOT normalized: it propagates so the job
    fails, the outbox row stays PENDING and ARQ/the sweeper retries with the
    truthful attribution instead of publishing a misattributed ``system`` hook.
    Read in its own short-lived session so it never depends on the indexing
    transaction.
    """
    if not outbox_id:
        return None, hook_origin.system_origin()
    try:
        outbox_uuid = uuid.UUID(outbox_id)
    except (ValueError, TypeError, AttributeError) as e:
        logger.warning("invalid outbox id %r for origin lookup: %s", outbox_id, e)
        return None, hook_origin.system_origin()
    async with async_session_maker() as session:
        row = (
            await session.execute(
                select(
                    IndexOutbox.actor_id,
                    IndexOutbox.origin_kind,
                    IndexOutbox.origin_name,
                    IndexOutbox.origin_task_id,
                ).where(IndexOutbox.id == outbox_uuid)
            )
        ).one_or_none()
    if row is None:
        return None, hook_origin.system_origin()
    actor_id, kind, name, task_id = row
    if kind is None:
        # Legacy/migration-era row: normalize fully to the explicit system shape
        # (null actor/task, D-13) — never pair a stale actor with a system origin.
        return None, hook_origin.system_origin()
    if kind in hook_origin.ALLOWED_KINDS and name:
        return actor_id, hook_origin.MutationOrigin(kind=kind, name=name, task_id=task_id)
    # Malformed persisted kind/name: fail safe to system, drop untrusted actor.
    return None, hook_origin.system_origin()


def _outbox_retry_job_id(job_name: str, object_id, outbox_id, attempts: int) -> str:
    """ARQ job id for one outbox retry attempt — attempt-qualified (EXT-06-T1).

    ``index``/``remove`` jobs carry the outbox row id so two objects' entries
    never collapse. That alone is not enough for a *retry*: ARQ treats a live
    *result* key as a duplicate too, and ``WorkerSettings.keep_result`` is 24h,
    so re-enqueueing the same outbox row with an unchanged id returns ``None``
    for a full day after the previous attempt completed — and the sweeper used
    to read that ``None`` as "already done". Appending the attempt counter
    makes every attempt a genuinely new job, so a retry always enqueues.
    """
    return f"{job_name}_{object_id}_{outbox_id}_a{int(attempts)}"


async def index_object_job(ctx: dict, object_id: str, outbox_id: str | None = None) -> dict:
    """Index a single Object into Elasticsearch via ARQ worker.

    Called from object_service create/update operations. ARQ auto-retries on ES
    unavailability per WorkerSettings.max_tries. CORESEAM-11/D-11: attribution
    comes from the persisted outbox row (by ``outbox_id``), so the index hooks
    name the original mutation initiator across first delivery and sweeper
    replay alike — never the worker process.

    The outbox row is marked DONE only when this call actually indexed the
    object (or the object is genuinely gone, the one explicit skip). A
    ``{"success": False}`` result is a failure — reporting DONE there would
    claim an index write that never happened (EXT-06-T1).
    """
    try:
        actor_id, origin = await _load_outbox_attribution(outbox_id)
        result = await index_object_svc(
            uuid.UUID(object_id), actor_id=actor_id, origin=origin
        )
        if result.get("success"):
            await _mark_outbox(outbox_id, IndexOutboxStatus.DONE.value)
            return result
        if result.get("error") == "Object not found":
            await _mark_outbox(outbox_id, IndexOutboxStatus.DONE.value)
            return {"success": True, "skipped": "Object not found (deleted)"}
        # Not DONE: the except branch below records the error, leaves the row
        # PENDING and re-raises so ARQ retries and the sweeper picks it up.
        raise RuntimeError(f"index_object failed: {result.get('error') or 'unknown error'}")
    except Exception as e:
        logger.error("index job error (object_id=%s): %s", object_id, e)
        # Back to PENDING (not FAILED) — ARQ itself will still retry; if that
        # retry also fails, the sweeper picks the entry up after its cooldown.
        await _mark_outbox(outbox_id, IndexOutboxStatus.PENDING.value, error=str(e))
        raise


async def remove_object_job(
    ctx: dict,
    object_id: str,
    object_type_id: str | None = None,
    schema_id: str | None = None,
    outbox_id: str | None = None,
) -> dict:
    """Remove an Object from Elasticsearch index.

    Called from object_service delete operations.
    object_type_id and schema_id are captured before DB deletion and passed as
    extra kwargs because the deleted Object can no longer supply them.
    """
    try:
        result = await remove_object_from_index(
            uuid.UUID(object_id),
            uuid.UUID(object_type_id) if object_type_id else None,
            uuid.UUID(schema_id) if schema_id else None,
        )
        await _mark_outbox(outbox_id, IndexOutboxStatus.DONE.value)
        return result
    except Exception as e:
        logger.error("remove index job error (object_id=%s): %s", object_id, e)
        await _mark_outbox(outbox_id, IndexOutboxStatus.PENDING.value, error=str(e))
        raise


async def index_object_type_job(ctx: dict, object_type_id: str) -> dict:
    """ARQ job: index an ObjectType in Elasticsearch."""
    try:
        result = await index_object_type(uuid.UUID(object_type_id))
        if not result.get("success") and result.get("error") == "ObjectType not found":
            return {"success": True, "skipped": "ObjectType not found (deleted)"}
        return result
    except Exception as e:
        logger.error("index object type job error (object_type_id=%s): %s", object_type_id, e)
        raise


async def remove_object_type_job(ctx: dict, object_type_id: str, schema_id: str) -> dict:
    """ARQ job: remove an ObjectType and its documents from Elasticsearch."""
    try:
        result = await remove_object_type_from_index(uuid.UUID(object_type_id))
        await remove_object_type_documents(uuid.UUID(object_type_id), uuid.UUID(schema_id))
        return result
    except Exception as e:
        logger.error("remove object type job error (object_type_id=%s): %s", object_type_id, e)
        raise


async def index_schema_job(ctx: dict, schema_id: str, outbox_id: str | None = None) -> dict:
    """ARQ job: index a Schema in Elasticsearch."""
    try:
        result = await index_schema(uuid.UUID(schema_id))
        if not result.get("success") and result.get("error") == "Schema not found":
            await _mark_outbox(outbox_id, IndexOutboxStatus.DONE.value)
            return {"success": True, "skipped": "Schema not found (deleted)"}
        await _mark_outbox(outbox_id, IndexOutboxStatus.DONE.value)
        return result
    except Exception as e:
        logger.error("index schema job error (schema_id=%s): %s", schema_id, e)
        await _mark_outbox(outbox_id, IndexOutboxStatus.PENDING.value, error=str(e))
        raise


async def remove_schema_job(ctx: dict, schema_id: str, outbox_id: str | None = None) -> dict:
    """ARQ job: remove a Schema from Elasticsearch."""
    try:
        result = await remove_schema_from_index(uuid.UUID(schema_id))
        await delete_index(uuid.UUID(schema_id))
        await _mark_outbox(outbox_id, IndexOutboxStatus.DONE.value)
        return result
    except Exception as e:
        logger.error("remove schema job error (schema_id=%s): %s", schema_id, e)
        await _mark_outbox(outbox_id, IndexOutboxStatus.PENDING.value, error=str(e))
        raise


async def outbox_sweeper_job(ctx: dict) -> dict:
    """Re-enqueue index/remove jobs whose outbox entry is still PENDING/ENQUEUED
    after a cooldown window — the self-healing half of the outbox pattern.

    Catches cases where the initial enqueue never happened (Redis was down)
    or the worker crashed before updating the outbox row. After enough failed
    attempts an entry is marked FAILED, which surfaces it as a dead-letter
    case for manual attention instead of retrying forever.
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=2)
    max_attempts = 10
    swept = 0
    failed = 0
    async with async_session_maker() as session:
        result = await session.execute(
            select(IndexOutbox).where(
                IndexOutbox.status.in_([IndexOutboxStatus.PENDING.value, IndexOutboxStatus.ENQUEUED.value]),
                IndexOutbox.created_at < cutoff,
            )
        )
        entries = result.scalars().all()
        if not entries:
            return {"swept": 0, "failed": 0, "checked": 0}

        pool = await get_arq_pool()
        for entry in entries:
            if entry.attempts >= max_attempts:
                entry.status = IndexOutboxStatus.FAILED.value
                entry.last_error = (
                    entry.last_error
                    or f"Outbox retries exhausted after {entry.attempts} attempts"
                )
                failed += 1
                continue
            if entry.operation in ("index_schema", "remove_schema"):
                job_name = f"{entry.operation}_job"
                kwargs = {}
            else:
                job_name = "index_object_job" if entry.operation == "index" else "remove_object_job"
                # Pre-migration remove rows have schema_id=None; preserve that
                # value so the service can perform its object lookup fallback.
                kwargs = (
                    {
                        "object_type_id": str(entry.object_type_id) if entry.object_type_id else None,
                        "schema_id": str(entry.schema_id) if entry.schema_id else None,
                    }
                    if entry.operation == "remove"
                    else {}
                )
            try:
                job = await pool.enqueue_job(
                    job_name, str(entry.object_id), outbox_id=str(entry.id), **kwargs,
                    # Attempt-qualified (EXT-06-T1): the outbox row id keeps
                    # different entries apart, the attempt counter keeps a RETRY
                    # from being deduped against the previous attempt's still-live
                    # result key (keep_result=86400), which made enqueue_job
                    # return None for a full day and silently stop retrying.
                    _job_id=_outbox_retry_job_id(
                        job_name, entry.object_id, entry.id, entry.attempts
                    ),
                )
                # ENQUEUED either way: a None here only means this exact attempt
                # id is already queued/in flight, which is still not proof the
                # index write happened. Only the job's own result may mark DONE
                # (index_object_job/remove_object_job), or exhaustion below FAILED.
                entry.status = IndexOutboxStatus.ENQUEUED.value
                entry.attempts += 1
                swept += 1
                if job is None:
                    logger.warning(
                        "outbox %s: attempt %s already enqueued, not claiming DONE",
                        entry.id, entry.attempts - 1,
                    )
            except Exception as e:
                entry.attempts += 1
                entry.last_error = str(e)
        await session.commit()
    return {"swept": swept, "failed": failed, "checked": len(entries)}


@with_audit_context
async def bulk_reindex_job(ctx: dict, object_type_id: str, task_id: str) -> dict:
    """Bulk reindex all Objects for an ObjectType.

    Called from POST /api/v1/search/reindex endpoint.
    Creates/recreates ES index with fresh mappings, then streams all objects.
    Task tracking follows process_import pattern.
    """
    async with async_session_maker() as session:
        try:
            ot_uuid = uuid.UUID(object_type_id)
            t_uuid = uuid.UUID(task_id)

            if not await task_service.start_task_run(session, t_uuid):
                return {"success": False, "error": "Task not running"}

            # Execute bulk reindex
            result = await bulk_reindex_svc(session, ot_uuid, t_uuid)

            # Mark task as SUCCESS
            task_update = TaskUpdate(
                current_status=TaskStatus.SUCCESS.value,
                current_step=TaskStep.POSTPROCESSING.value,
                completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            await task_service.update_task(session, t_uuid, task_update)

            return result
        except Exception as e:
            error_msg = task_service.safe_error_text(e, "bulk reindex failed")
            task_logger.error(
                "bulk reindex error (object_type_id=%s): %s", object_type_id, error_msg
            )
            try:
                await session.rollback()
                t_uuid = uuid.UUID(task_id)
                await task_service.mark_task_failed_or_dead_letter(session, t_uuid, error_msg, ctx)
            except Exception:
                pass
            return {"success": False, "error": error_msg}


@with_audit_context
async def reindex_all_job(ctx: dict, task_id: str) -> dict:
    """Reindex all ObjectTypes across every persisted schema."""
    async with async_session_maker() as session:
        try:
            t_uuid = uuid.UUID(task_id)

            # D-05/D-06: gated start — a task cancelled while still PENDING must
            # not be resurrected by this job (matches the other six task jobs).
            if not await task_service.start_task_run(session, t_uuid):
                return {"success": False, "error": "Task not running"}

            from app.services.search_service import delete_orphaned_schema_indices, reindex_schema

            result = await session.execute(select(Schema))
            schemas = result.scalars().all()
            total_indexed = 0
            total_errors = 0
            failed_schema_ids: set[str] = set()
            for schema in schemas:
                r = await reindex_schema(session, schema.id, t_uuid)
                total_indexed += r.get("indexed", 0)
                total_errors += r.get("errors", 0)
                if not r.get("success", True):
                    failed_schema_ids.add(str(schema.id))

            # Rebuild all live Schema indices before sweeping legacy per-Type indices.
            stale_deleted = 0
            if not failed_schema_ids:
                stale_deleted = await delete_orphaned_schema_indices(session)

            if not failed_schema_ids:
                task_status = TaskStatus.SUCCESS.value
                error_message = None
            elif len(failed_schema_ids) == len(schemas):
                task_status = TaskStatus.FAILED.value
                error_message = (
                    f"{len(failed_schema_ids)} of {len(schemas)} schema(s) failed to reindex: "
                    f"{sorted(failed_schema_ids)}"
                )
            else:
                task_status = TaskStatus.PARTIAL_SUCCESS.value
                error_message = (
                    f"{len(failed_schema_ids)} of {len(schemas)} schema(s) failed to reindex: "
                    f"{sorted(failed_schema_ids)}"
                )

            task_update = TaskUpdate(
                current_status=task_status,
                current_step=TaskStep.POSTPROCESSING.value,
                completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                **({"error_message": error_message} if error_message else {}),
            )
            await task_service.update_task(session, t_uuid, task_update)

            return {
                "success": not failed_schema_ids,
                "task_id": task_id,
                "schemas": len(schemas),
                "indexed": total_indexed,
                "stale_indices_deleted": stale_deleted,
                "errors": total_errors,
                "failed_schemas": sorted(failed_schema_ids),
            }
        except Exception as e:
            error_msg = task_service.safe_error_text(e, "reindex all failed")
            task_logger.error("reindex all error: %s", error_msg)
            try:
                await session.rollback()
                t_uuid = uuid.UUID(task_id)
                await task_service.mark_task_failed_or_dead_letter(session, t_uuid, error_msg, ctx)
            except Exception:
                pass
            raise


async def schema_reindex_job(ctx: dict, schema_id: str, task_id: str) -> dict:
    """Reindex all ObjectTypes under a Schema.

    Called from POST /api/v1/search/reindex/schema endpoint or scheduled job.
    Reads schema_id from args, iterates all ObjectTypes for the schema,
    and calls reindex_schema which delegates to bulk_reindex per ObjectType.
    Task tracking follows bulk_reindex_job pattern.
    """
    async with async_session_maker() as session:
        try:
            s_uuid = uuid.UUID(schema_id)
            t_uuid = uuid.UUID(task_id)

            if not await task_service.start_task_run(session, t_uuid):
                return {"success": False, "error": "Task not running"}

            # Execute schema reindex
            from app.services.search_service import reindex_schema
            result = await reindex_schema(session, s_uuid, t_uuid)

            # Derive the terminal status from reindex_schema()'s own outcome
            # (mirrors reindex_all_job's per-Schema aggregation, one level
            # down: per-ObjectType instead of per-Schema) — never report
            # SUCCESS for a reindex that reindex_schema() itself failed.
            success = result.get("success", True)
            processed = result.get("object_types_processed", 0)
            # Carry-03: the per-ObjectType detail from reindex_schema is already
            # a safe type+generic projection; the raw exception text never
            # reaches error_message.
            details = result.get("error_messages") or []
            if success:
                task_status = TaskStatus.SUCCESS.value
                error_message = None
            else:
                if processed > 0:
                    task_status = TaskStatus.PARTIAL_SUCCESS.value
                    base = (
                        f"{result.get('errors', 0)} error(s) during schema reindex; "
                        f"{processed} object type(s) processed successfully"
                    )
                else:
                    task_status = TaskStatus.FAILED.value
                    base = (
                        f"{result.get('errors', 0)} error(s) during schema reindex; "
                        "no object types processed successfully"
                    )
                error_message = task_service.bound_error_message(
                    base + (" — " + "; ".join(str(d) for d in details) if details else "")
                )

            task_update = TaskUpdate(
                current_status=task_status,
                current_step=TaskStep.POSTPROCESSING.value,
                completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                **({"error_message": error_message} if error_message else {}),
            )
            await task_service.update_task(session, t_uuid, task_update)

            return result
        except Exception as e:
            error_msg = task_service.safe_error_text(e, "schema reindex failed")
            task_logger.error(
                "schema reindex error (schema_id=%s): %s", schema_id, error_msg
            )
            try:
                await session.rollback()
                t_uuid = uuid.UUID(task_id)
                await task_service.mark_task_failed_or_dead_letter(session, t_uuid, error_msg, ctx)
            except Exception:
                pass
            return {"success": False, "error": error_msg}


# Phase 286 (PLUGTASK-02/D-13): the Core generic fallback persisted for any
# plugin task failure that does not supply an explicit safe business message.
PLUGIN_TASK_GENERIC_ERROR = (
    "The plugin task failed. See server logs for details."
)


class _DeclaredTaskRunner:
    """Binds one declaration's runner onto its owning plugin's identity.

    ``PluginRegistry.call_capability()`` resolves the callable via
    ``getattr(plugin, method_name)``. A declaration carries its own ``run``, so
    this thin adapter exposes it under the ``run`` attribute while preserving the
    owning plugin's manifest name for the cross-process active-state check. The
    plugin's own instance is never mutated, so a plugin declaring several types
    keeps one runner per declaration.
    """

    __slots__ = ("manifest", "run")

    def __init__(self, plugin, run) -> None:
        self.manifest = plugin.manifest
        self.run = run


@with_audit_context
async def run_plugin_task(ctx: dict, task_id: str) -> dict:
    """One static Core ARQ entrypoint for every plugin-declared Task type.

    The dynamic target comes from the worker-local task-type registry (never
    from ``WorkerSettings.functions``): the persisted Task's exact literal is
    resolved at execution time, its owning plugin is looked up in the
    worker-local ``PluginRegistry``, and the declared runner is invoked through
    the existing ``call_capability()`` timeout/exception chokepoint. Failures
    persist the Core failure/dead-letter outcome and return a JSON-safe failure
    envelope. Cancellation still propagates for ARQ's cancellation retry policy.
    """
    # Validate the id before a session exists so a malformed id can never
    # escape the try/finally below with an unclosed pooled connection.
    task_uuid = uuid.UUID(task_id)
    session = async_session_maker()
    plugin_token = None
    try:
        task = await task_service.get_task(session, task_uuid)
        if task is None:
            return {"success": False, "error": "Task not found"}

        # WR-01: an ARQ retry re-runs the SAME row, and update_task_progress
        # rejects a processed count below the persisted one — so each attempt
        # starts from a fresh progress baseline (monotonic within one attempt).
        # Phase 300 (D-05): a cancelled task is not started; start_task_run
        # writes its FAILED "Cancelled by user" and returns False.
        if not await task_service.start_task_run(session, task_uuid, reset_progress=True):
            return {"success": False, "error": "Task not running"}
        await session.refresh(task)

        entry = task_registry.get_worker_task_type_registry().resolve(task.tasktype)
        if entry is None:
            raise PluginTaskError(
                f"Task type {task.tasktype!r} is not dispatchable in this worker"
            )

        registry = get_plugin_registry()
        plugin = registry.get_plugin(entry.plugin_name)
        if plugin is None:
            raise PluginTaskError(
                f"Plugin {entry.plugin_name!r} is not loaded in this worker"
            )

        # D-04: run_plugin_task is a Core entry point — it binds the plugin name
        # and task type once the manifest is resolved; plugin code itself never
        # binds correlation ids.
        plugin_token = bind_log_context(
            plugin=entry.plugin_name, task_type=task.tasktype
        )

        plugin_context = await registry.build_context(
            entry.plugin_name,
            active_session=session,
            task_id=task_uuid,
            # D-08/D-09: the declaration's ordered steps govern report_step for
            # this attempt (one context per call = one per-attempt baseline).
            step_sequence=entry.declaration.steps,
            # CORESEAM-04: the SAME already-loaded row the wrapper marked RUNNING
            # above — the context's task-bound members never look a task up.
            task=task,
        )
        runner = _DeclaredTaskRunner(plugin, entry.declaration.run)
        # Only the persisted, validated parameter mapping reaches the runner.
        parameters = task.plugin_parameters or {}
        result = await registry.call_capability(
            runner,
            "run",
            plugin_context,
            task_uuid,
            **parameters,
            # D-286-08: the worker's ARQ job budget is the wrapper's inner
            # budget too — the 30 s DEFAULT_CAPABILITY_TIMEOUT would abort any
            # real plugin task while ARQ's own job_timeout stays the outer bound.
            timeout=JOB_TIMEOUT,
        )

        # MN-06: an ARQ cancellation retry re-runs the SAME row, so a prior
        # attempt's "Job cancelled or timed out" text must not survive success.
        # CORESEAM-04: a context-selected allowed success outcome (SUCCESS /
        # PARTIAL_SUCCESS) wins over the pre-289 SUCCESS default; the plugin can
        # never select a failure status (PluginContext's allowlist) and this
        # wrapper stays the only writer of the terminal row.
        selected_outcome = plugin_context.selected_outcome or TaskStatus.SUCCESS
        await task_service.update_task(
            session,
            task_uuid,
            TaskUpdate(
                current_status=selected_outcome.value,
                completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            ),
            clear_error_message=True,
        )

        # CORESEAM-04: after a SUCCESSFUL runner return, apply the deduplicated
        # declared + runtime cache invalidations. Deliberately fail-open and
        # ordered AFTER the terminal write above: the outcome is already
        # persisted, so a cache-infrastructure fault is logged and can never
        # rewrite it (T-289-05-03). A failing run skips this block entirely —
        # cache effects belong to a successful execution.
        requested_invalidations = tuple(
            dict.fromkeys(
                (
                    *entry.declaration.invalidates,
                    *plugin_context.requested_invalidations,
                )
            )
        )
        for entity_type in requested_invalidations:
            try:
                await invalidate_entity_cache(entity_type)
            except Exception as exc:
                task_logger.warning(
                    "plugin task cache invalidation failed for %r: %s",
                    entity_type,
                    exc,
                )

        return {
            "success": True,
            "task_id": task_id,
            "tasktype": task.tasktype,
            "result": result if isinstance(result, dict) else {},
        }
    except asyncio.CancelledError:
        if is_user_cancel_requested(task_uuid):
            # D-05/D-06: the with_cancel_monitor wrapper owns the terminal write
            # for a user cancel; this cleanup must not record a timeout status.
            try:
                await session.rollback()
            except Exception:
                pass
            raise
        task_logger.warning("plugin task cancelled/timed out")
        try:
            await session.rollback()
            await task_service.mark_task_failed_or_dead_letter(
                session, task_uuid, "Job cancelled or timed out", ctx
            )
        except Exception:
            pass
        raise
    except Exception as exc:
        # D-13: a PluginTaskError message is explicitly safe and may be
        # persisted verbatim; every other exception type yields the Core
        # generic message. Raw text stays in the log line only.
        if isinstance(exc, PluginCallError) and isinstance(
            exc.original, PluginTaskError
        ):
            safe_message = str(exc.original)
        elif isinstance(exc, PluginTaskError):
            safe_message = str(exc)
        else:
            safe_message = PLUGIN_TASK_GENERIC_ERROR
        # MJ-02: a plugin-authored message is bounded to the error_message
        # schema cap so the terminal write below can never fail on length.
        safe_message = task_service.bound_error_message(
            safe_message or PLUGIN_TASK_GENERIC_ERROR
        )
        task_logger.error("plugin task failed", exc_info=exc)
        try:
            await session.rollback()
            await task_service.mark_task_failed_or_dead_letter(
                session, task_uuid, safe_message, ctx
            )
        except Exception:
            pass
        return {"success": False, "task_id": task_id, "error": safe_message}
    finally:
        if plugin_token is not None:
            reset_log_context(plugin_token)
        await session.close()


class WorkerSettings:
    # Each arq job is wrapped outermost so with_audit_context's system-user
    # resolution runs inside the bound job context. _execute_scheduled_job takes
    # no arq ctx — it is wrapped at its definition by with_scheduler_log_context.
    functions: list = [
        with_log_context(with_cancel_monitor(process_import)),
        with_log_context(with_cancel_monitor(process_export)),
        with_log_context(with_cancel_monitor(process_folder_import)),
        with_log_context(index_object_job),
        with_log_context(remove_object_job),
        with_log_context(with_cancel_monitor(bulk_reindex_job)),
        with_log_context(with_cancel_monitor(schema_reindex_job)),
        with_log_context(with_cancel_monitor(reindex_all_job)),
        with_log_context(index_object_type_job),
        with_log_context(remove_object_type_job),
        with_log_context(index_schema_job),
        with_log_context(remove_schema_job),
        with_log_context(outbox_sweeper_job),
        with_log_context(with_cancel_monitor(run_plugin_task)),
        _execute_scheduled_job,
    ]
    redis_settings = _redis_settings
    job_serializer = json.dumps
    job_deserializer = json.loads
    on_startup = startup_recovery
    on_shutdown = shutdown
    # D-02/D-18: job_id and attempt are bound before arq logs "-> job(args)".
    on_job_start = bind_job_context
    # run_plugin_task returns plugin dicts; arq must not log their repr (T-298-17).
    log_results = False
    # Long-running plugin tasks (Jira sync work rate-limited one entity at a
    # time against the real Jira REST API) can legitimately take much longer
    # than a typical background job — the previous hardcoded 300s caused ARQ to
    # force-cancel still-healthy runs mid-flight. Cancellation raises
    # CancelledError (a BaseException in Python 3.8+), so every job that must
    # record a terminal status handles it explicitly. Configurable via
    # ARQ_JOB_TIMEOUT_SECONDS (0 = effectively unlimited, 24h).
    job_timeout: int = JOB_TIMEOUT
    max_tries: int = 5
    keep_result: int = 86400
    health_check_interval: int = 60
    health_check_key: str = "arq:worker:health"
