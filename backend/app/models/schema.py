import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, ForeignKey, JSON
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base
from app.models.language import Language


class Schema(Base):
    __tablename__ = "schemas"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(100), nullable=True, index=True)
    # 246 D-09: IndraMesh-native keygen prefix — distinct from `key` (Jira-occupied).
    keygen: Mapped[str] = mapped_column(String(100), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(String(1000), nullable=True)
    # Defaults mirror SchemaBase (app/schemas/crud.py) — required so ORM-direct
    # inserts that bypass the Pydantic schema don't violate these NOT NULL columns.
    primary_color: Mapped[str] = mapped_column(String(7), nullable=False, default="#2563EB")
    header_bg_color: Mapped[str] = mapped_column(String(7), nullable=False, default="#1E293B")
    sidebar_bg_color: Mapped[str] = mapped_column(String(7), nullable=False, default="#F1F5F9")
    section_bg_color: Mapped[str] = mapped_column(String(7), nullable=False, default="#F8FAFC")
    surface_bg_color: Mapped[str] = mapped_column(String(7), nullable=False, default="#FFFFFF")
    dark_primary_color: Mapped[str] = mapped_column(String(7), nullable=False, default="#3B82F6")
    dark_header_bg_color: Mapped[str] = mapped_column(String(7), nullable=False, default="#0F172A")
    dark_sidebar_bg_color: Mapped[str] = mapped_column(String(7), nullable=False, default="#1E293B")
    dark_section_bg_color: Mapped[str] = mapped_column(String(7), nullable=False, default="#0F172A")
    dark_surface_bg_color: Mapped[str] = mapped_column(String(7), nullable=False, default="#1E293B")
    status_filter_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    default_language_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("languages.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )

    source: Mapped["Source"] = relationship("Source", back_populates="schemas")
    object_types: Mapped[list["ObjectType"]] = relationship(
        "ObjectType", back_populates="schema", cascade="all, delete-orphan"
    )
    objects: Mapped[list["Object"]] = relationship("Object", back_populates="schema", cascade="all, delete-orphan")
    statuses: Mapped[list["Status"]] = relationship("Status", back_populates="schema", cascade="all, delete-orphan")
    imports: Mapped[list["Import"]] = relationship("Import", cascade="all, delete-orphan")
    exports: Mapped[list["Export"]] = relationship("Export", cascade="all, delete-orphan")
    targets: Mapped[list["Target"]] = relationship("Target", back_populates="schema", cascade="all, delete-orphan")
    icons: Mapped[list["Icon"]] = relationship("Icon", back_populates="schema", cascade="all, delete-orphan")
    default_language: Mapped["Language | None"] = relationship("Language")
    languages: Mapped[list["Language"]] = relationship(
        "Language", secondary="schema_languages", viewonly=True
    )

    @property
    def default_language_code(self) -> str | None:
        return self.default_language.code if self.default_language else None

    @property
    def multilingual(self) -> bool:
        """227 D-07: DERIVED — a schema is multilingual iff >= 2 languages are
        enabled in its subset. No stored flag (no migration, no toggle)."""
        return len(self.languages) >= 2