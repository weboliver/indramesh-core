import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Integer, Boolean, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base


class ObjectType(Base):
    __tablename__ = "object_types"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    schema_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("schemas.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("object_types.id", ondelete="CASCADE"), nullable=True
    )
    inherits_attributes: Mapped[bool] = mapped_column(Boolean, default=False)
    is_abstract: Mapped[bool] = mapped_column(Boolean, default=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(String(1000), nullable=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    label_attribute_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("object_type_attributes.id", ondelete="SET NULL"),
        nullable=True,
    )
    multilingual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    icon_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("icons.id", ondelete="SET NULL"), nullable=True
    )
    # 290 D-01: connector-neutral origin marker (free lowercase connector name,
    # e.g. "jira"); NULL is native. Never derived from a Jira id (D-12).
    managed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 290 D-02/D-06: generic write-protection marker set by the owning connector.
    externally_readonly: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )

    schema: Mapped["Schema"] = relationship("Schema", back_populates="object_types")
    parent: Mapped["ObjectType"] = relationship("ObjectType", remote_side="ObjectType.id", back_populates="children")
    children: Mapped[list["ObjectType"]] = relationship("ObjectType", back_populates="parent")
    attributes: Mapped[list["ObjectTypeAttribute"]] = relationship(
        "ObjectTypeAttribute", 
        back_populates="object_type",
        foreign_keys="ObjectTypeAttribute.object_type_id",
        cascade="all, delete-orphan"
    )
    objects: Mapped[list["Object"]] = relationship("Object", back_populates="object_type", cascade="all, delete-orphan")
    icon: Mapped["Icon"] = relationship("Icon", back_populates="object_types", lazy="selectin")
    label_attribute: Mapped["ObjectTypeAttribute"] = relationship(
        "ObjectTypeAttribute", foreign_keys=[label_attribute_id]
    )
    # cascade="all, delete-orphan" matches the DB-level ondelete="CASCADE" FK
    # on ExportDefinition/ImportDefinition.object_type_id (both NOT NULL).
    # Without it, SQLAlchemy's default relationship behavior tries to
    # disassociate child rows by setting object_type_id=NULL on delete,
    # which violates the NOT NULL constraint instead of letting the DB
    # cascade the delete.
    export_definitions: Mapped[list["ExportDefinition"]] = relationship(
        "ExportDefinition", back_populates="object_type", cascade="all, delete-orphan"
    )
    import_definitions: Mapped[list["ImportDefinition"]] = relationship(
        "ImportDefinition", back_populates="object_type", cascade="all, delete-orphan"
    )

    @property
    def icon_local_name(self) -> str | None:
        return self.icon.local_name if self.icon else None