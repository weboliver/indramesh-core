import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base
import enum


class AttributeTypeDataType(str, enum.Enum):
    DOUBLE = "double"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    TEXT = "text"
    DATETIME = "datetime"
    JSON = "json"
    STATUS = "status"
    VERSION = "version"
    GROUP = "group"
    USER = "user"
    JIRA = "jira"
    CONFLUENCE = "confluence"
    REFERENCE = "reference"
    SELECT = "select"
    URL = "url"
    EMAIL = "email"
    TEXTAREA = "textarea"


class AttributeType(Base):
    __tablename__ = "attribute_types"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    data_type: Mapped[AttributeTypeDataType] = mapped_column(
        String(50), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
