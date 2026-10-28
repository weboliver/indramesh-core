import uuid
from datetime import datetime, timezone
from sqlalchemy import Float, Integer, Boolean, DateTime, ForeignKey, Text, JSON
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base


class ObjectValue(Base):
    __tablename__ = "object_values"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    object_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("objects.id", ondelete="CASCADE"), nullable=False
    )
    object_type_attribute_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("object_type_attributes.id", ondelete="CASCADE"), nullable=False
    )
    value_float: Mapped[float] = mapped_column(Float, nullable=True)
    value_integer: Mapped[int] = mapped_column(Integer, nullable=True)
    value_boolean: Mapped[bool] = mapped_column(Boolean, nullable=True)
    value_simple: Mapped[str] = mapped_column(Text, nullable=True)
    value_datetime: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    value_struct: Mapped[dict] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )

    object: Mapped["Object"] = relationship("Object", back_populates="values")
    object_type_attribute: Mapped["ObjectTypeAttribute"] = relationship("ObjectTypeAttribute", back_populates="values")

    @property
    def attribute_name(self) -> str | None:
        if self.object_type_attribute:
            return self.object_type_attribute.name
        return None