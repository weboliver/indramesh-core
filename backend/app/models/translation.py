import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Text, DateTime, ForeignKey, UniqueConstraint, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base


class Translation(Base):
    __tablename__ = "translations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Polymorphic discriminator: points at the seeded translation_types row (D-14/D-16).
    translation_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("translation_types.id", ondelete="RESTRICT"), nullable=False
    )
    # Polymorphic entity reference: points at whatever table translation_type_id names.
    # Deliberately NO DB FK — cleanup is application-level via delete_translations_for() (D-15).
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    field: Mapped[str] = mapped_column(String(50), nullable=False)
    language_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("languages.id", ondelete="CASCADE"), nullable=False
    )
    value: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    translation_type: Mapped["TranslationType"] = relationship("TranslationType")
    language: Mapped["Language"] = relationship("Language")

    @property
    def language_code(self) -> str | None:
        return self.language.code if self.language else None

    __table_args__ = (
        UniqueConstraint("translation_type_id", "entity_id", "field", "language_id", name="uq_translations_natural_key"),
        Index("ix_translations_type_entity", "translation_type_id", "entity_id"),
    )
