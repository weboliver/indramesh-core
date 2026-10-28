import uuid
import enum
from datetime import datetime, timezone
from sqlalchemy import String, Integer, DateTime, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class IndexOutboxStatus(str, enum.Enum):
    PENDING = "PENDING"
    ENQUEUED = "ENQUEUED"
    DONE = "DONE"
    FAILED = "FAILED"


class IndexOutboxOperation(str, enum.Enum):
    INDEX = "index"
    REMOVE = "remove"
    INDEX_SCHEMA = "index_schema"
    REMOVE_SCHEMA = "remove_schema"


class IndexOutbox(Base):
    """Transactional outbox for Elasticsearch indexing jobs.

    Written in the same DB transaction as the Object mutation so a Redis
    outage or crashed worker can never silently lose an index/remove job —
    the outbox_sweeper_job re-enqueues anything left PENDING/ENQUEUED too long.

    object_id intentionally has no FK to objects.id: a "remove" entry refers
    to an Object that has already been deleted from Postgres by the time it
    is processed.

    ``actor_id``/``origin_kind``/``origin_name``/``origin_task_id`` persist the
    mutation provenance (CORESEAM-11/D-11). They are nullable only so
    pre-migration rows stay readable: a row with all four NULL is normalized to
    the explicit system/``core-index`` shape by the worker (D-13), never to a
    worker identity or an invented actor. ``actor_id`` deliberately has no FK —
    a "remove" row can outlive its user.
    """
    __tablename__ = "index_outbox"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    object_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    object_type_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    schema_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    operation: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    origin_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    origin_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    origin_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=IndexOutboxStatus.PENDING.value)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    processed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
