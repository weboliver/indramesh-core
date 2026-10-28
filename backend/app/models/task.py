import uuid
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import String, Integer, DateTime, ForeignKey, Text, Boolean
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base
import enum


class TaskStep(str, enum.Enum):
    SCHEMA = "SCHEMA"
    METADATA = "METADATA"
    STATUSES = "STATUSES"
    OBJECTTYPES = "OBJECTTYPES"
    ATTRIBUTES = "ATTRIBUTES"
    OBJECTS = "OBJECTS"
    VALUES = "VALUES"
    REFERENCES = "REFERENCES"
    POSTPROCESSING = "POSTPROCESSING"


class TaskStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"
    DEAD_LETTER = "DEAD_LETTER"  # ARQ retries exhausted — needs manual attention/replay


class TaskType(str, enum.Enum):
    # Phase 296 (CLEAN-01): the Core native task set is exactly these three.
    # Jira sync/export work is dispatched exclusively through the qualified
    # plugin wrapper and never through a Core task type.
    IMPORT = "IMPORT"
    EXPORT = "EXPORT"
    REINDEX = "REINDEX"


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    parent_task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True
    )
    import_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("imports.id", ondelete="CASCADE"), nullable=True
    )
    export_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("exports.id", ondelete="CASCADE"), nullable=True
    )
    schedule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("schedules.id", ondelete="CASCADE"), nullable=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=True
    )
    schema_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("schemas.id", ondelete="CASCADE"), nullable=True
    )
    object_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("object_types.id", ondelete="CASCADE"), nullable=True
    )
    target_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("targets.id", ondelete="CASCADE"), nullable=True
    )
    target: Mapped[Optional["Target"]] = relationship("Target", foreign_keys=[target_id])
    export_definition_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("export_definitions.id", ondelete="SET NULL"), nullable=True
    )
    import_definition_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("import_definitions.id", ondelete="SET NULL"), nullable=True
    )
    service_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_accounts.id", ondelete="SET NULL"), nullable=True
    )
    # Phase 291.1 (CORESEAM-10, D-04): the persisted ORIGINAL creator of this
    # task — the authenticated user whose admission created it. A scheduled
    # clone inherits the permanent template's creator and a retry inherits the
    # original row's creator, so a task-bound plugin SDK write always audits
    # the same actor regardless of who (or what) triggered the run. Nullable
    # with no backfill: legacy rows and system-created tasks have no reliable
    # creator and must fail closed instead of inventing one. Set only by Core
    # (never in TaskCreate/TaskUpdate) and ON DELETE SET NULL so removing a
    # user leaves the task runnable, just unable to attribute SDK writes.
    creator_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Phase 286 (D-01/D-10, PLUGTASK-01): tasktype/current_step are persisted as
    # bounded strings, not PostgreSQL native enums. Core values keep their
    # TaskType/TaskStep enum constants for legacy dispatch branches; plugin tasks
    # carry a qualified `<plugin>.<task>` string. current_step is nullable so a
    # freshly created plugin task has no business step until run() reports one —
    # Core creation flows still send "SCHEMA" explicitly, preserving the old
    # default through the request schema rather than the column.
    current_step: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_status: Mapped[TaskStatus] = mapped_column(
        String(20), default=TaskStatus.PENDING
    )
    sort: Mapped[int] = mapped_column(Integer, default=0)
    tasktype: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    # Phase 300 (D-05): a user cancel is a REQUEST, not an immediate terminal
    # write. The API sets this timestamp; the worker's cancel monitor notices it,
    # stops the run, lets it flush its logs and only then records FAILED. Never
    # settable through TaskCreate/TaskUpdate (both stay extra=forbid).
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_permanent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Phase 247 (UX-04, D-12..D-15): server-observed, worker/service-only
    # progress counters. NULL means unknown/not yet observed; a persisted 0
    # is a real known value distinct from NULL. Only
    # app.services.task_service.update_task_progress ever writes these —
    # the public TaskCreate/TaskUpdate schemas forbid the fields entirely
    # (see app/schemas/crud.py); they are exposed only via TaskResponse.
    processed_objects: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_objects: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Phase 286 (D-04..D-06, PLUGTASK-02): immutable, validated plugin start
    # parameters. Nullable with no default — Core tasks and plugin tasks without
    # declared parameters store NULL. Written only through TaskCreate (never
    # TaskUpdate) and copied verbatim by retry so a queued/running/retried task
    # always has reproducible input. No secrets: sensitive config stays in the
    # Fernet-encrypted plugin config namespace.
    plugin_parameters: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    source: Mapped[Optional["Source"]] = relationship(
        "Source", foreign_keys=[source_id]
    )
    schema: Mapped[Optional["Schema"]] = relationship(
        "Schema", foreign_keys=[schema_id]
    )
    linked_object_type: Mapped[Optional["ObjectType"]] = relationship(
        "ObjectType", foreign_keys=[object_type_id]
    )
    service_account: Mapped[Optional["ServiceAccount"]] = relationship(
        "ServiceAccount", foreign_keys=[service_account_id]
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )