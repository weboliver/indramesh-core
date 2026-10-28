import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base


class Object(Base):
    __tablename__ = "objects"
    __table_args__ = (
        UniqueConstraint("schema_id", "key", name="uq_objects_schema_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    schema_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("schemas.id", ondelete="CASCADE"), nullable=False
    )
    object_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("object_types.id", ondelete="CASCADE"), nullable=False
    )
    # 246 D-05: human-readable immutable IndraMesh key, unique per schema (D-06).
    key: Mapped[str] = mapped_column(String(255), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )

    schema: Mapped["Schema"] = relationship("Schema", back_populates="objects")
    object_type: Mapped["ObjectType"] = relationship("ObjectType", back_populates="objects")
    values: Mapped[list["ObjectValue"]] = relationship("ObjectValue", back_populates="object", cascade="all, delete-orphan")
    references_from: Mapped[list["ObjectRef"]] = relationship(
        "ObjectRef", foreign_keys="ObjectRef.from_object_id", back_populates="from_object", cascade="all, delete-orphan"
    )
    references_to: Mapped[list["ObjectRef"]] = relationship(
        "ObjectRef", foreign_keys="ObjectRef.to_object_id", back_populates="to_object", cascade="all, delete-orphan"
    )
    # 278-03 (D-12, Pitfall 6): ORM cascade mirrors the DB-level ON DELETE
    # CASCADE so delete_object() can read obj.files (storage_keys) before the
    # rows disappear. Both mechanisms are required together.
    files: Mapped[list["ObjectFile"]] = relationship("ObjectFile", back_populates="object", cascade="all, delete-orphan")
    comments: Mapped[list["ObjectComment"]] = relationship("ObjectComment", back_populates="object", cascade="all, delete-orphan")