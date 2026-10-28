"""Rollback-safe, bounded task-log handler and writer.

The handler runs synchronously on the logging call: it accepts only the records
Carry-01 allows (INFO+, a task/app/connector.*/plugin.* category, and a
Core-bound ``task_id``) and appends an already-redacted, already-truncated
entry to an in-process buffer. It performs no DB I/O and no event-loop call.

* (``task.id``, ``sequence``) is the primary key.
* ``TaskLogWriter`` is the single async task per worker process. Every second it
  drains all buffers through its own ``AsyncSession`` — never the job session —
  so a job rollback cannot erase an emitted line (Carry-02). ``flush_task``
  persists one task's buffer and only returns once it is committed, which is the
  D-07 barrier used by ``task_service`` before a terminal status commits. A DB
  outage keeps the buffers and retries with bounded backoff, never dropping an
  accepted line. The cap (D-09/D-10) keeps memory bounded at ``head + tail``.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import re
import threading
import uuid
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from types import MappingProxyType

from app.logging.categories import category_for
from app.logging.context import get_log_context
from app.logging.redaction import redact_text
from app.services.task_log_service import (
    DEFAULT_MAX_LINES,
    LogEntry,
    persist_batch,
)

_HANDLER_MARKER = "_indramesh_task_log_handler"

MAX_MESSAGE_BYTES = 1024
MAX_DETAILS_BYTES = 512

# Only these structured extras survive into ``details`` (Carry-03). Everything
# else a caller may have put on a record is ignored.
ALLOWED_DETAIL_KEYS = ("error_count", "processed_objects", "total_objects")
DETAIL_REQUIRED_BYTES_KEY = "exception_type"

# Category acceptance (Carry-01): only the task-adjacent Core and plugin
# categories may reach the DB. ``db``/``access``/``security``/``llm`` never do.
_ACCEPTED_CATEGORIES = ("task", "app")
_ACCEPTED_PREFIXES = ("connector.", "plugin.")
_GENERIC_EXCEPTION_MESSAGE = "The task failed. See server logs for details."

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Set while the writer itself logs, so its own safe stdout warnings can never be
# captured back into task_logs and recurse.
_SUPPRESSED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "indramesh_task_log_writer_suppressed", default=False
)


def _mask_controls(text: str) -> str:
    return _CONTROL_RE.sub(" ", text)


def _truncate_utf8(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", "ignore")


def _exception_type(record: logging.LogRecord) -> str | None:
    if record.exc_info:
        exc_type = record.exc_info[0]
        if exc_type is not None:
            return getattr(exc_type, "__name__", str(exc_type))
    args = record.args
    if isinstance(args, BaseException):
        return type(args).__name__
    if isinstance(args, tuple):
        values = args
    elif isinstance(args, MappingProxyType) or isinstance(args, dict):
        values = list(args.values())
    else:
        values = ()
    for value in values:
        if isinstance(value, BaseException):
            return type(value).__name__
    return None


def _safe_details(record: logging.LogRecord, exc_type: str | None) -> dict | None:
    details: dict = {}
    if exc_type:
        details[DETAIL_REQUIRED_BYTES_KEY] = exc_type
    for key in ALLOWED_DETAIL_KEYS:
        value = record.__dict__.get(key)
        # ``bool`` is an ``int`` subclass; a count is never a boolean.
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        details[key] = value
    if not details:
        return None
    if len(json.dumps(details).encode("utf-8")) <= MAX_DETAILS_BYTES:
        return details
    for key in reversed([k for k in ALLOWED_DETAIL_KEYS if k in details]):
        details.pop(key, None)
        if len(json.dumps(details).encode("utf-8")) <= MAX_DETAILS_BYTES:
            break
    return details or None


def build_safe_entry(record: logging.LogRecord) -> LogEntry | None:
    """Project a record into a safe entry, or return None if it is not accepted.

    ``task_id`` always comes from the Core log context, never from record extras
    (T-300-02). Redaction runs before the byte cap so a truncated message can
    never expose a partial secret (T-300-01).
    """
    if record.levelno < logging.INFO:
        return None
    if _SUPPRESSED.get():
        return None

    category = category_for(record.name)
    if not (category in _ACCEPTED_CATEGORIES or category.startswith(_ACCEPTED_PREFIXES)):
        return None

    context = get_log_context()
    task_uuid = _as_uuid(context.get("task_id"))
    if task_uuid is None:
        return None

    attempt = context.get("attempt")
    if not isinstance(attempt, int) or attempt < 1:
        attempt = 1

    exc_type = _exception_type(record)
    if exc_type is not None:
        # Never persist a raw exception message: only its type plus a generic
        # Core message, with the type kept in the allow-listed details.
        message = f"{exc_type}: {_GENERIC_EXCEPTION_MESSAGE}"
    else:
        try:
            raw_message = record.getMessage()
        except Exception:
            raw_message = str(record.msg)
        message = redact_text(raw_message)
    message = _truncate_utf8(_mask_controls(message), MAX_MESSAGE_BYTES)

    return LogEntry(
        task_id=task_uuid,
        emitted_at=datetime.now(timezone.utc).replace(tzinfo=None),
        attempt=attempt,
        level=int(record.levelno),
        category=category,
        logger=record.name,
        message=message,
        details=_safe_details(record, exc_type),
    )


class TaskLogHandler(logging.Handler):
    """Root-logger handler that buffers accepted records for the writer."""

    def __init__(self, writer: "TaskLogWriter") -> None:
        super().__init__()
        self._writer = writer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = build_safe_entry(record)
            if entry is None:
                return
            self._writer.accept(entry)
        except Exception:
            self.handleError(record)


class _TaskBuffer:
    """Bounded in-memory buffer for one task.

    Holds at most ``head`` entries from the start and a ``deque(maxlen=tail)``
    for the rest, so memory stays bounded during a long DB outage. Anything the
    middle drops is counted as ``dropped_pending`` and added on the next
    successful persist.
    """

    __slots__ = (
        "head_cap",
        "tail_cap",
        "head",
        "tail",
        "dropped_pending",
        "segment",
        "segment_attempt",
        "current_step",
        "pending_bump",
        "closed",
    )

    def __init__(self, cap: int) -> None:
        self.head_cap = cap // 2
        self.tail_cap = cap - self.head_cap
        self.head: list[LogEntry] = []
        self.tail: deque[LogEntry] = deque(maxlen=self.tail_cap)
        self.dropped_pending = 0
        self.segment = 0
        self.segment_attempt: int | None = None
        self.current_step: str | None = None
        self.pending_bump = False
        self.closed = False

    def set_step(self, step: str | None) -> None:
        if step == self.current_step:
            return
        self.current_step = step
        if self.segment != 0:
            self.pending_bump = True

    def append(self, entry: LogEntry) -> LogEntry:
        if self.segment == 0:
            self.segment = 1
            self.segment_attempt = entry.attempt
        elif entry.attempt != self.segment_attempt:
            self.segment += 1
            self.segment_attempt = entry.attempt
            self.pending_bump = False
        elif self.pending_bump:
            self.segment += 1
            self.pending_bump = False

        stamped = replace(entry, step=self.current_step, segment=self.segment)
        if len(self.head) < self.head_cap:
            self.head.append(stamped)
        else:
            before = len(self.tail)
            self.tail.append(stamped)
            if before == self.tail_cap:
                self.dropped_pending += 1
        return stamped

    def snapshot(self) -> tuple[list[LogEntry], int]:
        return list(self.head) + list(self.tail), self.dropped_pending

    def commit(self, entries: list[LogEntry], dropped: int) -> None:
        ids = {id(entry) for entry in entries}
        self.head = [e for e in self.head if id(e) not in ids]
        self.tail = deque(
            (e for e in self.tail if id(e) not in ids), maxlen=self.tail_cap
        )
        self.dropped_pending = max(0, self.dropped_pending - dropped)


@dataclass(frozen=True)
class TaskLogConfig:
    max_lines: int = DEFAULT_MAX_LINES
    retention_days: int = 30

    @classmethod
    def from_env(cls, env: dict | None = None) -> "TaskLogConfig":
        source = os.environ if env is None else env
        return cls(
            max_lines=_env_int(source, "TASK_LOG_MAX_LINES", DEFAULT_MAX_LINES, 2, 10000),
            retention_days=_env_int(source, "TASK_LOG_RETENTION_DAYS", 30, 1, 3650),
        )


def _env_int(env, name: str, default: int, minimum: int, maximum: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"{name} must be an integer between {minimum} and {maximum}"
        ) from None
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _as_uuid(value) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


class TaskLogWriter:
    """One per worker process: buffers task logs and persists them periodically."""

    def __init__(
        self,
        session_factory,
        *,
        config: TaskLogConfig | None = None,
        interval: float = 1.0,
        max_backoff: float = 30.0,
        sleeper=None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config or TaskLogConfig()
        self._interval = float(interval)
        self._max_backoff = float(max_backoff)
        self._sleeper = sleeper or asyncio.sleep
        self._logger = logger or logging.getLogger("app.logging.task_log")
        self._buffer_lock = threading.Lock()
        self._tasks: dict[uuid.UUID, _TaskBuffer] = {}
        self._flush_locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._handler: TaskLogHandler | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._closed = False
        self._last_warning = 0.0

    @property
    def config(self) -> TaskLogConfig:
        return self._config

    # -- handler lifecycle -------------------------------------------------

    def install_handler(self) -> None:
        root = logging.getLogger()
        for existing in list(root.handlers):
            if getattr(existing, _HANDLER_MARKER, False):
                root.removeHandler(existing)
        if self._handler is None:
            self._handler = TaskLogHandler(self)
        setattr(self._handler, _HANDLER_MARKER, True)
        root.addHandler(self._handler)

    def uninstall_handler(self) -> None:
        root = logging.getLogger()
        if self._handler is not None and self._handler in root.handlers:
            root.removeHandler(self._handler)

    # -- buffering (synchronous, no DB I/O) --------------------------------

    def accept(self, entry: LogEntry) -> None:
        if self._closed:
            return
        with self._buffer_lock:
            buf = self._tasks.get(entry.task_id)
            if buf is None:
                buf = _TaskBuffer(self._config.max_lines)
                self._tasks[entry.task_id] = buf
            # D-07: a sealed task accepts no further records; they go to stdout
            # only. This is the in-process counterpart of the old (wrong)
            # state.expired write — the buffer's own flag is authoritative.
            if buf.closed:
                return
            buf.append(entry)

    def set_step(self, task_id, step: str | None) -> None:
        if self._closed:
            return
        key = _as_uuid(task_id)
        if key is None:
            return
        with self._buffer_lock:
            buf = self._tasks.get(key)
            if buf is None:
                buf = _TaskBuffer(self._config.max_lines)
                self._tasks[key] = buf
            if buf.closed:
                return
            buf.set_step(step)

    # -- persistence -------------------------------------------------------

    def _flush_lock(self, task_id: uuid.UUID) -> asyncio.Lock:
        lock = self._flush_locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            self._flush_locks[task_id] = lock
        return lock

    async def _persist(
        self, task_id: uuid.UUID, entries: list[LogEntry], dropped: int, *, seal: bool
    ) -> None:
        async with self._session_factory() as session:
            try:
                await persist_batch(
                    session,
                    task_id,
                    entries,
                    dropped,
                    cap=self._config.max_lines,
                    seal=seal,
                )
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    def _backoff(self, attempt: int) -> float:
        return min(self._interval * (2 ** min(attempt, 10)), self._max_backoff)

    def _warn_outage(self, task_id: uuid.UUID, pending: int, exc: Exception) -> None:
        loop = asyncio.get_running_loop()
        now = loop.time()
        if now - self._last_warning < 60.0:
            return
        self._last_warning = now
        token = _SUPPRESSED.set(True)
        try:
            self._logger.warning(
                "task log writer: database unavailable (%s); %d accepted line(s) buffered",
                type(exc).__name__,
                pending,
            )
        finally:
            _SUPPRESSED.reset(token)

    async def flush(self, task_id, *, seal: bool = False) -> None:
        """Persist one task's buffer with bounded retry; seal closes it (D-07)."""
        key = _as_uuid(task_id)
        if key is None:
            return
        async with self._flush_lock(key):
            with self._buffer_lock:
                buf = self._tasks.get(key)
                if buf is None:
                    if not seal:
                        return
                    buf = _TaskBuffer(self._config.max_lines)
                    self._tasks[key] = buf
                entries, dropped = buf.snapshot()
            if not entries and dropped == 0 and not seal:
                return

            attempt = 0
            while True:
                try:
                    await self._persist(key, entries, dropped, seal=seal)
                except Exception as exc:
                    attempt += 1
                    self._warn_outage(key, len(entries), exc)
                    await self._sleeper(self._backoff(attempt))
                    continue
                with self._buffer_lock:
                    current = self._tasks.get(key)
                    if current is not None:
                        current.commit(entries, dropped)
                        if seal:
                            current.closed = True
                return

    async def flush_task(self, task_id) -> None:
        """D-07 barrier: persist and seal this task before it may reach a terminal status."""
        await self.flush(task_id, seal=True)

    async def _run(self) -> None:
        while True:
            # The periodic tick is real wall-clock time; the injectable
            # ``sleeper`` is the retry-backoff seam used by ``flush`` so tests
            # never wait out an outage.
            await asyncio.sleep(self._interval)
            if not self._running:
                return
            with self._buffer_lock:
                keys = list(self._tasks.keys())
            for key in keys:
                if not self._running:
                    return
                try:
                    await self.flush(key)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A per-task failure already retried/backed off inside
                    # flush(); never let one task stop the others.
                    pass

    async def start(self) -> None:
        if self._running:
            return
        self._closed = False
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._closed = True
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        with self._buffer_lock:
            keys = list(self._tasks.keys())
        for key in keys:
            try:
                await self.flush(key, seal=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        self.uninstall_handler()


_writer: TaskLogWriter | None = None


def get_task_log_writer() -> TaskLogWriter | None:
    """Return the process-wide writer, or None outside the worker process."""
    return _writer


def set_task_log_writer(writer: TaskLogWriter) -> None:
    global _writer
    _writer = writer


def clear_task_log_writer(writer: TaskLogWriter | None = None) -> None:
    global _writer
    if writer is None or _writer is writer:
        _writer = None
