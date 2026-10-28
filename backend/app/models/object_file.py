import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ObjectFile(Base):
    """Metadata row for one blob stored in the internal storage service (D-11/D-12).

    FK on object_id is ON DELETE CASCADE (DB level) and the ORM relationship on
    Object.files carries cascade="all, delete-orphan" — both are required so
    object_service.delete_object() can capture storage_key values before the
    rows vanish (RESEARCH Pitfall 6).
    """

    __tablename__ = "object_files"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    object_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("objects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    storage_key: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    uploader_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # 290 D-11/D-12: same single-source-of-truth origin marker as ObjectComment.
    managed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 290 D-13: externally-provided attachment author (login/id string), stored
    # as plain text so the API can render authorship without fabricating a User.
    external_author_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )

    object: Mapped["Object"] = relationship("Object", back_populates="files")