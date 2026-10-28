import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, Boolean, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False)
    is_service_account: Mapped[bool] = mapped_column(Boolean, default=False)
    # D-09 — per-user token version ("log out everywhere"): every minted token
    # carries the value current at mint time, and get_current_user refuses a
    # token whose claim disagrees with this row. Bumped by a password change
    # and by POST /auth/revoke-all. An integer, not a timestamp: a
    # second-granularity epoch has a truncation race an integer does not.
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )

    # passive_deletes=True: both FKs already carry ON DELETE CASCADE at the DB
    # level (see UserProfile.user_id / UserRole.user_id below). Without this,
    # SQLAlchemy's default ORM-level cascade behavior loads the related rows
    # on parent delete and tries to NULL their FK column itself instead of
    # trusting the DB cascade — which fails with a NotNullViolationError here
    # since user_id is NOT NULL, breaking DELETE /api/v1/users/{id} for any
    # user that has a profile (Rule 3 fix, surfaced by Phase 232 E2E cleanup).
    profile: Mapped["UserProfile"] = relationship("UserProfile", back_populates="user", uselist=False, passive_deletes=True)
    roles: Mapped[list["UserRole"]] = relationship("UserRole", back_populates="user", passive_deletes=True)


class UserProfile(Base):
    __tablename__ = "user_profiles"

    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_profile_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    first_name: Mapped[str] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str] = mapped_column(String(100), nullable=True)
    avatar_url: Mapped[str] = mapped_column(String(500), nullable=True)
    ui_language: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )

    user: Mapped["User"] = relationship("User", back_populates="profile")