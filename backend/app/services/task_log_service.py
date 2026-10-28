"""Persistence for core task logs.

The writer hands over already-redacted, already-truncated entries; this module
owns the storage-side mechanics: sequence assignment, the head/tail cap, and the
absolute per-task counters on ``task_log_state`` (D-09/D-10).

``persist_batch`` runs in the caller's short transaction and commits nothing
itself — the ``TaskLogWriter`` owns the session and the commit. ``append_task_log``
uses the same logic inside a *foreign* transaction so a backend or recovery line
can be committed together with a status change.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, distinct, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import Task, TaskStatus
from app.models.task_log import TaskLog, TaskLogState
from app.schemas.task_log import TaskLogEntry, TaskLogGap, TaskLogPage

DEFAULT_MAX_LINES = 10000

# Stored levels are numeric; the API projects canonical names (D-15). The
# minimum-level filter maps the three UI choices (D-04) to numeric thresholds.
_LEVEL_NAMES: dict[int, str] = {
    10: "DEBUG",
    20: "INFO",
    30: "WARNING",
    40: "ERROR",
    50: "CRITICAL",
}
MIN_LEVEL_VALUES: dict[str, int] = {"INFO": 20, "WARNING": 30, "ERROR": 40}

# One read transaction sees the state row and its lines from the same snapshot
# (D-09/D-10 consistency); the purge job shares this isolation.
_READ_ISOLATION = {"isolation_level": "REPEATABLE READ"}


@dataclass(frozen=True, slots=True)
class LogEntry:
    """One immutable, already-safe accepted log line."""

    task_id: uuid.UUID
    emitted_at: datetime
    attempt: int
    level: int
    category: str
    logger: str
    message: str
    step: str | None = None
    segment: int = 1
    details: dict | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _get_locked_state(
    session: AsyncSession, task_id: uuid.UUID, cap: int, *, create: bool
) -> TaskLogState | None:
    state = (
        await session.execute(
            select(TaskLogState)
            .where(TaskLogState.task_id == task_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if state is None and create:
        state = TaskLogState(
            task_id=task_id,
            next_sequence=0,
            dropped_count=0,
            pruning_revision=0,
            expired=False,
            max_lines=cap,
        )
        session.add(state)
        await session.flush()
    return state


async def _write_entries(
    session: AsyncSession,
    task_id: uuid.UUID,
    entries: list[LogEntry],
    dropped: int,
    *,
    cap: int,
    seal: bool = False,
) -> bool:
    state = await _get_locked_state(
        session, task_id, cap, create=bool(entries) or seal
    )
    if state is None:
        return False
    if state.expired:
        # The task was sealed by an earlier flush_task; later lines go to
        # stdout only (D-07 barrier already satisfied).
        return False

    effective_cap = state.max_lines or cap
    head = effective_cap // 2
    tail = effective_cap - head
    next_sequence = int(state.next_sequence or 0)

    for offset, entry in enumerate(entries, start=1):
        session.add(
            TaskLog(
                task_id=task_id,
                sequence=next_sequence + offset,
                emitted_at=entry.emitted_at,
                attempt=entry.attempt,
                segment=entry.segment,
                step=entry.step,
                level=entry.level,
                category=entry.category,
                logger=entry.logger,
                message=entry.message,
                details=entry.details,
            )
        )
    new_next = next_sequence + len(entries)
    await session.flush()

    deleted = 0
    if new_next > effective_cap:
        result = await session.execute(
            delete(TaskLog).where(
                TaskLog.task_id == task_id,
                TaskLog.sequence > head,
                TaskLog.sequence <= new_next - tail,
            )
        )
        deleted = result.rowcount or 0

    state.next_sequence = new_next
    dropped_total = int(dropped or 0)
    state.dropped_count = (
        int(state.dropped_count or 0) + dropped_total + deleted
    )
    pruned_total = deleted + dropped_total
    if pruned_total:
        state.pruning_revision = int(state.pruning_revision or 0) + pruned_total
    # NOTE: `seal` here only forces creation of the state row. The D-07
    # "closed for further records" behaviour lives in the in-process writer
    # (TaskLogWriter buffer.closed) — it must NOT be represented by
    # `state.expired`, which is the retention/purge flag (D-11) and would
    # otherwise mark every completed task as expired on the UI.
    await session.flush()
    return True


async def persist_batch(
    session: AsyncSession,
    task_id: uuid.UUID,
    entries: list[LogEntry],
    dropped: int,
    *,
    cap: int = DEFAULT_MAX_LINES,
    seal: bool = False,
) -> bool:
    """Insert a writer batch, prune the discarded middle, and bump the counters.

    Idempotent under a lost commit response: the caller keeps the same batch and
    retries, re-reading ``next_sequence`` from the locked state row, so a rolled
    back attempt reuses the same sequences and writes each line exactly once.
    """
    return await _write_entries(
        session, task_id, entries, dropped, cap=cap, seal=seal
    )


async def append_task_log(
    session: AsyncSession,
    task_id: uuid.UUID,
    level: int,
    message: str,
    *,
    category: str = "task",
    logger_name: str = "task.lifecycle",
    attempt: int = 1,
    step: str | None = None,
    details: dict | None = None,
    cap: int = DEFAULT_MAX_LINES,
) -> None:
    """Write one direct log line inside the caller's transaction.

    Intended for backend/recovery lines that must commit together with a status
    change. The caller owns the commit; this never commits on its own.
    """
    entry = LogEntry(
        task_id=task_id,
        emitted_at=_now(),
        attempt=attempt,
        level=level,
        category=category,
        logger=logger_name,
        message=message,
        step=step,
        segment=1,
        details=details,
    )
    await _write_entries(session, task_id, [entry], 0, cap=cap)


def _level_name(level: int) -> str:
    return _LEVEL_NAMES.get(int(level), str(level))


def _entry_from_row(row: TaskLog) -> TaskLogEntry:
    emitted = row.emitted_at
    if emitted.tzinfo is None:
        emitted = emitted.replace(tzinfo=timezone.utc)
    return TaskLogEntry(
        sequence=int(row.sequence),
        emitted_at=emitted,
        attempt=int(row.attempt),
        segment=int(row.segment),
        step=row.step,
        level=_level_name(row.level),  # type: ignore[arg-type]
        category=row.category,
        logger=row.logger,
        message=row.message,
        details=row.details,
    )


def _gap_from_state(state: TaskLogState, next_sequence: int, stored_count: int):
    """Describe the discarded middle using the frozen cap (D-09/D-10).

    ``missing`` counts sequences that were assigned but are no longer stored.
    In-memory-only drops never receive sequences, so ``missing`` stays 0 and no
    gap is reported for them.
    """
    missing = next_sequence - stored_count
    if missing <= 0:
        return None
    cap = int(state.max_lines or DEFAULT_MAX_LINES)
    head = cap // 2
    tail = cap - head
    after_sequence = head
    before_sequence = next_sequence - tail + 1
    if before_sequence <= after_sequence + 1:
        return None
    return TaskLogGap(
        after_sequence=after_sequence,
        before_sequence=before_sequence,
        count=missing,
    )


async def _load_state(
    session: AsyncSession, task_id: uuid.UUID
) -> TaskLogState | None:
    return (
        await session.execute(
            select(TaskLogState).where(TaskLogState.task_id == task_id)
        )
    ).scalar_one_or_none()


async def read_task_logs(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    after: int = 0,
    limit: int = 200,
    min_level: str = "INFO",
) -> TaskLogPage | None:
    """Read one cursor page in a single repeatable-read transaction.

    Returns ``None`` when the Task does not exist (route -> 404). A Task that
    exists without any state row is a legacy/never-written log: 200, empty,
    ``expired=False``. A fully purged Task keeps its state row with
    ``expired=True`` and returns 200, empty (D-11).
    """
    await session.connection(execution_options=_READ_ISOLATION)

    task_exists = (
        await session.execute(select(Task.id).where(Task.id == task_id))
    ).scalar_one_or_none()
    if task_exists is None:
        return None

    state = await _load_state(session, task_id)

    rows = (
        await session.execute(
            select(TaskLog)
            .where(TaskLog.task_id == task_id, TaskLog.sequence > after)
            .order_by(TaskLog.sequence)
            .limit(limit)
        )
    ).scalars().all()

    # ``next_after`` is the highest SCANNED sequence, so a poll whose lines were
    # all filtered out still advances (D-04) instead of stalling.
    next_after = int(rows[-1].sequence) if rows else after

    more_row = (
        await session.execute(
            select(TaskLog.sequence)
            .where(TaskLog.task_id == task_id, TaskLog.sequence > next_after)
            .limit(1)
        )
    ).first()
    has_more = more_row is not None

    stored_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(TaskLog)
                .where(TaskLog.task_id == task_id)
            )
        ).scalar_one()
    )
    attempt_count = int(
        (
            await session.execute(
                select(func.count(distinct(TaskLog.attempt))).where(
                    TaskLog.task_id == task_id
                )
            )
        ).scalar_one()
    )

    threshold = MIN_LEVEL_VALUES.get(min_level, MIN_LEVEL_VALUES["INFO"])
    items = [_entry_from_row(row) for row in rows if int(row.level) >= threshold]

    if state is None:
        return TaskLogPage(
            items=items,
            next_after=next_after,
            has_more=has_more,
            pruning_revision=0,
            dropped_count=0,
            stored_count=stored_count,
            attempt_count=attempt_count,
            expired=False,
            gap=None,
        )

    next_sequence = int(state.next_sequence or 0)
    return TaskLogPage(
        items=items,
        next_after=next_after,
        has_more=has_more,
        pruning_revision=int(state.pruning_revision or 0),
        dropped_count=int(state.dropped_count or 0),
        stored_count=stored_count,
        attempt_count=attempt_count,
        expired=bool(state.expired),
        gap=_gap_from_state(state, next_sequence, stored_count),
    )


def _format_download_line(row: TaskLog) -> str:
    emitted = row.emitted_at
    if emitted.tzinfo is None:
        emitted = emitted.replace(tzinfo=timezone.utc)
    timestamp = emitted.isoformat().replace("+00:00", "Z")
    step = row.step if row.step else "-"
    return (
        f"{timestamp}  attempt={row.attempt}  step={step}  "
        f"{_level_name(row.level)}  {row.category}  {row.message}"
    )


async def render_task_log_download(
    session: AsyncSession, task_id: uuid.UUID
) -> str | None:
    """Render the full stored log as chronological plain text (D-16).

    Ignores ``min_level`` entirely, reads every retained line plus the state in
    one transaction, and places the dropped-lines marker at its position. The
    language is fixed English (machine artifact, not localized).
    """
    await session.connection(execution_options=_READ_ISOLATION)

    task_exists = (
        await session.execute(select(Task.id).where(Task.id == task_id))
    ).scalar_one_or_none()
    if task_exists is None:
        return None

    state = await _load_state(session, task_id)
    rows = (
        await session.execute(
            select(TaskLog)
            .where(TaskLog.task_id == task_id)
            .order_by(TaskLog.sequence)
        )
    ).scalars().all()

    marker: str | None = None
    before_sequence: int | None = None
    if state is not None:
        next_sequence = int(state.next_sequence or 0)
        stored_count = len(rows)
        missing = next_sequence - stored_count
        if missing > 0:
            cap = int(state.max_lines or DEFAULT_MAX_LINES)
            head = cap // 2
            tail = cap - head
            after_sequence = head
            candidate = next_sequence - tail + 1
            if candidate > after_sequence + 1:
                marker = (
                    f"----- {missing} lines dropped "
                    "(line limit reached) -----"
                )
                before_sequence = candidate

    lines: list[str] = []
    marker_written = False
    for row in rows:
        if marker is not None and not marker_written and row.sequence >= before_sequence:
            lines.append(marker)
            marker_written = True
        lines.append(_format_download_line(row))
    if marker is not None and not marker_written:
        lines.append(marker)

    if not lines:
        return ""
    return "\n".join(lines) + "\n"


async def purge_task_logs(
    session_factory,
    *,
    now: datetime | None = None,
    retention_days: int = 30,
    batch_size: int = 1000,
) -> int:
    """Delete log lines older than the retention window (D-11/D-12).

    PENDING and RUNNING tasks are never touched, so an active long run keeps its
    history. A finished task whose lines all disappear is marked
    ``expired=True`` and its ``pruning_revision`` is raised; the Task row and the
    state row stay. Deleting a Task still removes its logs via the FK cascade.
    Idempotent: a second run over the same window deletes nothing.
    """
    current = now or _now()
    cutoff = current - timedelta(days=retention_days)
    active_ids = select(Task.id).where(
        Task.current_status.in_(
            [TaskStatus.PENDING.value, TaskStatus.RUNNING.value]
        )
    )

    total_deleted = 0
    while True:
        async with session_factory() as session:
            task_ids = list(
                (
                    await session.execute(
                        select(TaskLog.task_id)
                        .where(
                            TaskLog.emitted_at < cutoff,
                            TaskLog.task_id.notin_(active_ids),
                        )
                        .distinct()
                        .limit(batch_size)
                    )
                ).scalars().all()
            )
            if not task_ids:
                await session.commit()
                break

            result = await session.execute(
                delete(TaskLog).where(
                    TaskLog.task_id.in_(task_ids),
                    TaskLog.emitted_at < cutoff,
                )
            )
            deleted = int(result.rowcount or 0)

            for task_id in task_ids:
                remaining = int(
                    (
                        await session.execute(
                            select(func.count())
                            .select_from(TaskLog)
                            .where(TaskLog.task_id == task_id)
                        )
                    ).scalar_one()
                )
                if remaining:
                    continue
                state = await session.get(TaskLogState, task_id)
                if state is not None:
                    state.expired = True
                    state.pruning_revision = int(state.pruning_revision or 0) + 1

            await session.commit()
            total_deleted += deleted
            if deleted < batch_size:
                break

    return total_deleted
