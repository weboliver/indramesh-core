import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ObjectComment(Base):
    """Free-text comment on an Object (D-12: no storage-service reference).

    FK on object_id is ON DELETE CASCADE (DB level) and the ORM relationship on
    Object.comments carries cascade="all, delete-orphan" — both are required so
    SC-04's row cleanup is complete on Object delete (RESEARCH Pitfall 6).
    """

    __tablename__ = "object_comments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    object_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("objects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # The externally-provided author display name for a connector-managed
    # comment. Stored as plain text so the API/frontend can render authorship
    # WITHOUT resolving or auto-creating a local User — author_id stays
    # permanently NULL for these rows and the DTO never falls back to
    # "System". NULL for every native, locally-authored comment.
    external_author_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 290 D-11/D-12: single source of truth for comment provenance — NULL is
    # native, non-NULL (e.g. "jira") is externally managed and delete-guarded.
    managed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )

    object: Mapped["Object"] = relationship("Object", back_populates="comments")