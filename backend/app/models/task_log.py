import uuid
from datetime import datetime
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class TaskLog(Base):
    """One persisted task log line, already redacted and truncated.

    Written exclusively by ``TaskLogWriter`` through its own DB session, never
    the job session (Carry-02), so a job rollback cannot erase an emitted line.
    ``sequence`` is assigned at persist time and forms the composite primary key
    with ``task_id``; the head/tail cap prunes the discarded middle instead of
    renumbering surviving rows (D-09/D-10). ``emitted_at`` is naive UTC, like the
    other task timestamps.
    """
    __tablename__ = "task_logs"

    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    emitted_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    segment: Mapped[int] = mapped_column(Integer, nullable=False)
    step: Mapped[str | None] = mapped_column(String(255), nullable=True)
    level: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    category: Mapped[str] = mapped_column(String(255), nullable=False)
    logger: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class TaskLogState(Base):
    """Per-task write cursor, cap and closure bookkeeping.

    ``next_sequence`` is the next sequence to hand out. ``dropped_count`` and
    ``pruning_revision`` are absolute counters, never deltas, so a retried
    commit applies them idempotently. ``max_lines`` is frozen on the first write
    from ``TaskLogConfig`` (D-10). ``expired`` marks a task closed to further DB
    writes after ``flush_task`` returned; later records go to stdout only.
    """
    __tablename__ = "task_log_state"

    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    next_sequence: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    dropped_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pruning_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expired: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    max_lines: Mapped[int | None] = mapped_column(Integer, nullable=True)
