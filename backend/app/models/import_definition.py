import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, ForeignKey, JSON, UniqueConstraint, Boolean
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base


class ImportDefinition(Base):
    __tablename__ = "import_definitions"
    __table_args__ = (
        UniqueConstraint("name", name="uq_import_definition_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    object_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("object_types.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    field_mappings: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    identity_attr_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("object_type_attributes.id", ondelete="SET NULL"), nullable=True
    )
    value_separator: Mapped[str] = mapped_column(String(10), default=";")
    date_format: Mapped[str | None] = mapped_column(String(50), nullable=True)
    encoding: Mapped[str] = mapped_column(String(20), default="utf-8")
    is_permanent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )

    object_type: Mapped["ObjectType"] = relationship("ObjectType", back_populates="import_definitions")
    source: Mapped["Source"] = relationship("Source", back_populates="import_definitions")
