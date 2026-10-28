import uuid
import enum
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, ForeignKey, UniqueConstraint, Boolean, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class RecordEntityType(str, enum.Enum):
    SOURCE = "source"
    SCHEMA = "schema"
    OBJECT_TYPE = "object_type"
    ATTRIBUTE_TYPE = "attribute_type"
    OBJECT = "object"


class RecordPermission(Base):
    """Record-level (row-level) permission entry for one role on one specific
    record. record_id intentionally has no FK: it points into one of 5
    different tables (sources, schemas, object_types, attribute_types,
    objects) depending on record_entity_type — see IndexOutbox.object_id /
    SyncAuditLog.entity_id for the same established pattern in this codebase.
    Absence of a row for a given (role, record) pair means ALLOW (permissive
    default) — resolved in Phase 130, not encoded here."""

    __tablename__ = "record_permissions"

    __table_args__ = (
        UniqueConstraint(
            "role_id", "record_entity_type", "record_id", "action",
            name="uq_record_permission",
        ),
        Index("ix_record_permissions_entity", "record_entity_type", "record_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), nullable=False
    )
    record_entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    resource: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    is_deny: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
