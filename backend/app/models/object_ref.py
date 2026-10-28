import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base


class ObjectRef(Base):
    __tablename__ = "object_refs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    from_object_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("objects.id", ondelete="CASCADE"), nullable=False
    )
    to_object_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("objects.id", ondelete="CASCADE"), nullable=False
    )
    object_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("object_types.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    from_attribute_name: Mapped[str] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    from_object: Mapped["Object"] = relationship(
        "Object", foreign_keys=[from_object_id], back_populates="references_from"
    )
    to_object: Mapped["Object"] = relationship(
        "Object", foreign_keys=[to_object_id], back_populates="references_to"
    )
    object_type: Mapped["ObjectType"] = relationship("ObjectType")
    source: Mapped["Source"] = relationship("Source")

    @property
    def to_object_name(self) -> str | None:
        if self.to_object:
            return self.to_object.name
        return None

    @property
    def from_object_name(self) -> str | None:
        if self.from_object:
            return self.from_object.name
        return None