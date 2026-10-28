import enum
import uuid
from datetime import datetime, timezone as tz
from typing import Optional
from sqlalchemy import String, Boolean, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base
from app.models.task import TaskType


class SchedulePresetType(str, enum.Enum):
    EVERY_N_MINUTES = "EVERY_N_MINUTES"
    EVERY_N_HOURS = "EVERY_N_HOURS"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    job_function: Mapped[str] = mapped_column(String(100), nullable=True)
    cron_expression: Mapped[str] = mapped_column(String(50), nullable=False)
    args: Mapped[dict] = mapped_column(JSON, nullable=True, default={})
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=True
    )
    schema_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("schemas.id", ondelete="CASCADE"), nullable=True
    )
    object_type_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("object_types.id", ondelete="CASCADE"), nullable=True
    )
    target_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("targets.id", ondelete="CASCADE"), nullable=True
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    template_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # The authenticated user whose create request produced this row — a
    # server-authored ownership marker, never settable through ScheduleCreate/
    # ScheduleUpdate (261022-np1). Nullable with no backfill: legacy rows and
    # system-created schedules stay creatorless and are superuser-only. ON
    # DELETE SET NULL keeps a schedule running (superuser-only) after its
    # creator is removed instead of cascading the schedule away.
    creator_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    timezone: Mapped[str] = mapped_column(String(50), nullable=False, default="UTC")
    # Phase 286 (PLUGTASK-01): shares the Task contract's 255-char bound. Only
    # the Core schedulable set is ever written here (schedule_service keeps its
    # closed validation) — the wider column exists so historic plugin values can
    # be stored/displayed safely without a narrowing ALTER on downgrade.
    tasktype: Mapped[str] = mapped_column(String(255), nullable=False, default=TaskType.IMPORT.value)
    preset_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_run_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(tz.utc).replace(tzinfo=None))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(tz.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(tz.utc).replace(tzinfo=None)
    )

    source: Mapped[Optional["Source"]] = relationship("Source", foreign_keys=[source_id])
    schema: Mapped[Optional["Schema"]] = relationship("Schema", foreign_keys=[schema_id])
    object_type: Mapped[Optional["ObjectType"]] = relationship("ObjectType", foreign_keys=[object_type_id])
    target: Mapped[Optional["Target"]] = relationship("Target", foreign_keys=[target_id])
