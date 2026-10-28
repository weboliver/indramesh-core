"""Thin shared types for connector package — no ABC hierarchy per Pitfall 2."""

from dataclasses import dataclass, field


@dataclass
class PushResult:
    """Result of a single object push operation by a connector."""
    success: bool = False
    error: str | None = None
    conflict: bool = False
    remote_updated_at: str | None = None
    fields_pushed: int = 0
    fields_failed: int = 0
    field_errors: list[dict] = field(default_factory=list)
    # Each field_error: {"entity_type": "object_value"|"object_ref", "entity_id": str, "reason": str}
