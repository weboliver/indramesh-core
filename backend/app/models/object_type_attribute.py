import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Integer, Boolean, DateTime, ForeignKey, Text, JSON
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base


class ObjectTypeAttribute(Base):
    __tablename__ = "object_type_attributes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    object_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("object_types.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(String(1000), nullable=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    attribute_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("attribute_types.id", ondelete="RESTRICT"), nullable=False
    )
    is_required: Mapped[bool] = mapped_column(Boolean, default=False)
    multilingual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    minimum_cardinality: Mapped[int] = mapped_column(Integer, default=0)
    maximum_cardinality: Mapped[int] = mapped_column(Integer, default=-1)
    is_unique: Mapped[bool] = mapped_column(Boolean, default=False)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    # 246 D-03: explicit system-OTA protection flag — never a name/type
    # convention. System OTAs are non-deletable and non-renamable.
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    default_value: Mapped[str] = mapped_column(Text, nullable=True)
    structure: Mapped[dict] = mapped_column(JSON, nullable=True)
    referenced_object_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("object_types.id", ondelete="CASCADE"), nullable=True
    )
    reference_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reference_types.id", ondelete="SET NULL"), nullable=True
    )
    # 290 D-01: same connector-neutral origin marker as ObjectType.
    managed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 290 D-02: attribute-level lock; a value is locked when the requested type
    # OR this attribute carries the marker (D-02).
    externally_readonly: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )

    object_type: Mapped["ObjectType"] = relationship(
        "ObjectType", 
        back_populates="attributes",
        foreign_keys=[object_type_id]
    )
    attribute_type: Mapped["AttributeType"] = relationship("AttributeType")
    referenced_object_type: Mapped["ObjectType"] = relationship("ObjectType", foreign_keys=[referenced_object_type_id])
    reference_type: Mapped["ReferenceType"] = relationship("ReferenceType", foreign_keys=[reference_type_id])
    values: Mapped[list["ObjectValue"]] = relationship("ObjectValue", back_populates="object_type_attribute", cascade="all, delete-orphan")