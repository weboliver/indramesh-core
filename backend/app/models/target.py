import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, DateTime, ForeignKey, JSON
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base


class Target(Base):
    __tablename__ = "targets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    schema_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("schemas.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    target_url: Mapped[str] = mapped_column(String(500), nullable=False)
    config: Mapped[dict] = mapped_column(JSON, nullable=True)
    export_folder: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    # CORESEAM-01 (Phase 289 Plan 01): registry-validated string identity. The
    # Python-side "jira" default is gone deliberately — no write path may
    # materialize an implicit connector; the API requires an explicit,
    # registered value. varchar(50) matches the registry's identifier grammar
    # and migration 289_connector_type_registry.
    connector_type: Mapped[str] = mapped_column(String(50), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )

    schema: Mapped["Schema"] = relationship("Schema", back_populates="targets")
    export_definitions: Mapped[list["ExportDefinition"]] = relationship("ExportDefinition", back_populates="target")
    service_accounts: Mapped[list["ServiceAccount"]] = relationship(
        "ServiceAccount", back_populates="target", cascade="all, delete-orphan"
    )