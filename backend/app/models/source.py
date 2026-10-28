import uuid
from datetime import datetime, timezone
from sqlalchemy import BigInteger, DateTime, FetchedValue, JSON, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # CORESEAM-01 (Phase 289 Plan 03): the connector identity is an OPEN
    # lowercase string (registry-validated on writes only). There is
    # deliberately no Python enum any more — a persisted identifier whose
    # connector is unknown/disabled must stay readable.
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_url: Mapped[str] = mapped_column(String(500), nullable=True)
    # Phase 292 Plan 02 (D-04): connector-neutral URL provenance. A Core-owned
    # BEFORE UPDATE trigger (migration 292_source_url_revision) is the only
    # writer — it advances this by 1 per genuine source_url transition, even
    # while the Jira plugin is disabled and even if a caller forges a value.
    # server_default keeps a fresh Source at 0; server_onupdate=FetchedValue()
    # tells the ORM not to write it and to re-read the DB-computed value.
    source_url_revision: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("0"),
        server_onupdate=FetchedValue(),
    )
    config: Mapped[dict] = mapped_column(JSON, nullable=True)
    import_folder: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )

    schemas: Mapped[list["Schema"]] = relationship("Schema", back_populates="source", cascade="all, delete-orphan")
    service_accounts: Mapped[list["ServiceAccount"]] = relationship(
        "ServiceAccount", back_populates="source", cascade="all, delete-orphan"
    )
    import_definitions: Mapped[list["ImportDefinition"]] = relationship(
        "ImportDefinition", back_populates="source", cascade="all, delete-orphan"
    )