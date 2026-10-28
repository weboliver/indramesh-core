"""Read/download schemas for the Core task log (TLOG-03/TLOG-04, D-15/D-16).

The API contract is consumed by the task-detail log view (plan 04). Levels are
stored as numeric logging levels and projected here as their canonical names;
``TaskLogPage`` always carries ``pruning_revision`` so a client can detect that
retained history was pruned and reload from ``after=0`` (D-09/D-10).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

LogLevelName = Literal["INFO", "WARNING", "ERROR", "CRITICAL"]


class TaskLogEntry(BaseModel):
    """One persisted log line, already redacted and truncated on write."""

    sequence: int
    emitted_at: datetime
    attempt: int
    segment: int
    step: str | None = None
    level: LogLevelName
    category: str
    logger: str
    message: str
    details: dict[str, Any] | None = None


class TaskLogGap(BaseModel):
    """The discarded middle of a capped log (D-09/D-10).

    ``after_sequence`` is the last retained line before the gap and
    ``before_sequence`` the first retained line after it; ``count`` is how many
    lines are missing in between.
    """

    after_sequence: int
    before_sequence: int
    count: int


class TaskLogPage(BaseModel):
    """One cursor page of stored log lines plus the retention metadata."""

    items: list[TaskLogEntry]
    next_after: int
    has_more: bool
    pruning_revision: int
    dropped_count: int
    stored_count: int
    attempt_count: int
    expired: bool
    gap: TaskLogGap | None = None
