from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field


class ObjectFileResponse(BaseModel):
    """Read-only ObjectFile API DTO (279 plan 01 tracer).

    Deliberately excludes storage_key and uploader_id: the storage UUID is
    internal (D-22), and the uploader is exposed as a resolved display name
    produced by the service query (R-06), never a raw identifier.
    """

    id: UUID
    filename: str = Field(..., max_length=500)
    content_type: Optional[str] = None
    size_bytes: int
    uploader_display_name: Optional[str] = None
    created_at: datetime
    # 290 D-11/D-12/D-14: managed_by is the single origin source (free-form
    # lowercase connector name, e.g. "jira"; NULL is native). is_external is
    # derived from it, never a stored column.
    managed_by: Optional[str] = Field(default=None, max_length=64)

    @computed_field
    @property
    def is_external(self) -> bool:
        return self.managed_by is not None

    model_config = ConfigDict(from_attributes=True)