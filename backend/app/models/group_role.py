import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base


class GroupRole(Base):
    """groups_roles join table — group -> role assignment (246 D-15).

    Mirrors the UserRole m:n pattern (role.py). Permissions flow ONLY
    groups -> roles (groups_roles) -> role_permissions; there is deliberately
    NO direct groups <-> permissions connection (D-15).
    """

    __tablename__ = "groups_roles"

    __table_args__ = (
        UniqueConstraint("group_id", "role_id", name="uq_group_role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), nullable=False
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    group: Mapped["Group"] = relationship("Group")
    role: Mapped["Role"] = relationship("Role")