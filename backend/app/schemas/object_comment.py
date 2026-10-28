from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator


def sanitize_comment_body(v: str) -> str:
    """Trim + harden one comment body before persistence (D-07/D-08/D-09/WR-02).

    Extracted verbatim from ObjectCommentCreate._trim_and_validate (Phase 281
    Task 1) so a connector comment import path and the native user-submitted
    path share ONE validation implementation — never a duplicated copy that
    can drift (closes the WR-02 bug class PATTERNS.md flags: an unhardened
    connector insert would surface the same unhandled asyncpg
    CharacterNotInRepertoireError/DataError as a 500).
    """
    # D-07: reject an empty or whitespace-only body after trimming.
    trimmed = v.strip()
    if not trimmed:
        raise ValueError("Comment body must not be empty or whitespace-only")
    # WR-02 (280-REVIEW.md): reject characters PostgreSQL/UTF-8 cannot
    # store before they reach asyncpg. A NUL byte survives strip() and
    # raises CharacterNotInRepertoireError; a lone UTF-16 surrogate is
    # valid JSON and survives strip() but raises DataError on encode.
    # Both would otherwise surface as an unhandled 500 instead of a 422.
    if "\x00" in trimmed:
        raise ValueError("Comment body must not contain NUL characters")
    try:
        trimmed.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(
            "Comment body contains characters that cannot be stored"
        ) from None
    # D-09: the 64,000-character boundary is evaluated on the TRIMMED
    # value, never the raw pre-trim length.
    if len(trimmed) > 64000:
        raise ValueError("Comment body must not exceed 64000 characters")
    # D-08: boundary whitespace is removed before persistence; internal
    # whitespace and line breaks are preserved.
    return trimmed


class ObjectCommentCreate(BaseModel):
    """Create DTO for an Object comment (D-07/D-08/D-09).

    Deliberately declares no author_id field (T-280-06): authorship comes
    exclusively from the authenticated current_user. The post-trim length
    boundary lives in the field_validator — a `Field(max_length=64000)` on
    the raw value would reject a technically-valid (after-trim) submission
    carrying excess boundary whitespace, contradicting the trim-then-persist
    contract (D-08).
    """

    body: str = Field(...)

    @field_validator("body")
    @classmethod
    def _trim_and_validate(cls, v: str) -> str:
        # Phase 281 Task 1: thin wrapper over the module-level helper so a
        # connector import path can reuse the identical boundary/hardening logic.
        return sanitize_comment_body(v)


class ObjectCommentResponse(BaseModel):
    """Read-only ObjectComment API DTO (280-02 tracer).

    Never exposes author_id as a raw UUID (D-06): the service resolves
    author_display_name through the same User/UserProfile fallback chain as
    ObjectFileResponse.uploader_display_name. For a connector-managed comment
    (is_external=True, 290 D-12/D-14) the SAME author_display_name field
    carries the stored external_author_name — no second DTO field.
    """

    id: UUID
    body: str
    author_display_name: Optional[str] = None
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