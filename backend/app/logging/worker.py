"""Per-job log-context wrappers for arq jobs and APScheduler-driven calls.

Every wrapper sets **all** ``JOB_FIELDS`` (missing ones to ``None``) before the
job body runs and restores the previous context in a ``finally`` block, so no
line ever inherits an id from the process start or from a previous job (D-02).
``job_id`` is always taken from the arq ``ctx`` and never from a same-named job
parameter.

Binding happens only at Core entry points (this module and ``run_plugin_task``);
plugin code itself never binds correlation ids (D-04).
"""

from __future__ import annotations

import inspect
import logging
from functools import wraps
from typing import Any, Callable

from app.logging.context import bind_log_context, reset_log_context
from app.logging.fields import JOB_FIELDS

logger = logging.getLogger(__name__)


def resolve_trigger(
    attempt: int | None, schedule_id: str | None, *, has_task: bool
) -> str | None:
    """Return the job's trigger source or ``None`` when it cannot be known.

    D-03: a job without a task and without a scheduler origin stays empty rather
    than guessing. A re-run (``attempt > 1``) is always a retry, regardless of
    origin.
    """
    if attempt is not None and attempt > 1:
        return "retry"
    if not has_task:
        return None
    if schedule_id:
        return "schedule"
    return "manual"


async def _read_task_correlation(task_id: str):
    """Read a task's ``tasktype``/``schema_id``/``schedule_id`` for correlation.

    One short read-only session per task job. Any failure (unparseable id, no
    row, DB hiccup) is logged at DEBUG and never fails the job — correlation is
    best-effort, the job's own work is not.
    """
    try:
        from sqlalchemy import select

        from app.db import async_session_maker
        from app.models.task import Task

        async with async_session_maker() as session:
            row = (
                await session.execute(
                    select(
                        Task.tasktype, Task.schema_id, Task.schedule_id
                    ).where(Task.id == task_id)
                )
            ).one_or_none()
            return row
    except Exception:
        logger.debug("task correlation lookup failed for %s", task_id, exc_info=True)
        return None


def _base_values() -> dict[str, Any]:
    return dict.fromkeys(JOB_FIELDS)


def with_log_context(fn: Callable) -> Callable:
    """Wrap an arq job so every line carries the job's own correlation fields.

    ``inspect.signature`` follows ``__wrapped__``, so the original parameter
    names of an already-decorated job (``with_audit_context``) are still
    resolved.
    """
    signature = inspect.signature(fn)

    @wraps(fn)
    async def wrapper(ctx: dict, *args: Any, **kwargs: Any) -> Any:
        bound = signature.bind_partial(ctx, *args, **kwargs).arguments
        values = _base_values()
        values["job_id"] = ctx.get("job_id")
        values["job_name"] = fn.__name__
        values["attempt"] = ctx.get("job_try")

        raw_task_id = bound.get("task_id")
        task_id = str(raw_task_id) if raw_task_id is not None else None
        values["task_id"] = task_id

        raw_schema_id = bound.get("schema_id")
        schema_id = str(raw_schema_id) if raw_schema_id is not None else None

        schedule_id: str | None = None
        if task_id:
            row = await _read_task_correlation(task_id)
            if row is not None:
                tasktype, row_schema_id, row_schedule_id = row
                values["task_type"] = tasktype
                if schema_id is None and row_schema_id is not None:
                    schema_id = str(row_schema_id)
                if row_schedule_id is not None:
                    schedule_id = str(row_schedule_id)
        # A parameter value wins over the persisted row's schema_id.
        values["schema_id"] = schema_id
        values["schedule_id"] = schedule_id
        values["trigger"] = resolve_trigger(
            values["attempt"], schedule_id, has_task=bool(task_id)
        )

        token = bind_log_context(**values)
        try:
            return await fn(ctx, *args, **kwargs)
        finally:
            reset_log_context(token)

    wrapper.__indramesh_log_context__ = True
    return wrapper


def with_scheduler_log_context(fn: Callable) -> Callable:
    """Wrap an APScheduler-driven coroutine (no arq ``ctx``).

    All correlation fields are reset; ``trigger`` is ``schedule`` and
    ``schedule_id`` is filled from a same-named parameter when present. The
    module-level name stays, so persisted job-store references
    (``app.services.scheduler_manager:<name>``) keep resolving.
    """
    signature = inspect.signature(fn)

    @wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        bound = signature.bind_partial(*args, **kwargs).arguments
        values = _base_values()
        values["job_name"] = fn.__name__
        values["trigger"] = "schedule"
        raw_schedule_id = bound.get("schedule_id")
        if raw_schedule_id is not None:
            values["schedule_id"] = str(raw_schedule_id)

        token = bind_log_context(**values)
        try:
            return await fn(*args, **kwargs)
        finally:
            reset_log_context(token)

    wrapper.__indramesh_log_context__ = True
    return wrapper


async def bind_job_context(ctx: dict) -> None:
    """arq ``on_job_start`` hook: bind ``job_id`` and ``attempt`` per job.

    arq runs each job in its own ``run_job`` task whose context is discarded
    afterwards, so the token is deliberately not reset here.
    """
    values = _base_values()
    values["job_id"] = ctx.get("job_id")
    values["attempt"] = ctx.get("job_try")
    bind_log_context(**values)
