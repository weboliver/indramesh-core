"""Strict DTOs for the runtime logging-level API (D-01/D-02/D-23)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from app.logging.discovery import LoggingDiscoveryUnavailable
from app.logging.levels import (
    MAX_CATEGORIES_PER_REQUEST,
    InvalidLevelError,
    InvalidLevelSnapshot,
    normalize_level,
)


class LogLevelOverride(BaseModel):
    """One PUT entry: an explicit level plus an optional relative duration.

    ``extra="forbid"`` rejects an absolute ``expires_at`` (D-23). ``ttl_seconds``
    is a strict integer (no bool/float/string coercion) in 60..86400 and may be
    omitted above the TTL threshold — but an explicit ``null`` is a 422.
    """

    model_config = ConfigDict(extra="forbid")

    level: str
    ttl_seconds: StrictInt | None = None

    @field_validator("level")
    @classmethod
    def _normalize_level(cls, value: str) -> str:
        try:
            return normalize_level(value)
        except InvalidLevelError as exc:
            raise ValueError(str(exc)) from exc

    @model_validator(mode="after")
    def _reject_explicit_null_ttl(self) -> "LogLevelOverride":
        if "ttl_seconds" in self.model_fields_set and self.ttl_seconds is None:
            raise ValueError("ttl_seconds must be an integer when provided")
        return self


class LogLevelsUpdate(BaseModel):
    """The PUT envelope: a partial category -> override map (never a full replace)."""

    model_config = ConfigDict(extra="forbid")

    levels: dict[str, LogLevelOverride | None]

    @field_validator("levels")
    @classmethod
    def _limit_map(cls, value: dict) -> dict:
        if not value:
            raise ValueError("levels must contain at least one category")
        if len(value) > MAX_CATEGORIES_PER_REQUEST:
            raise ValueError(
                f"levels must contain at most {MAX_CATEGORIES_PER_REQUEST} categories"
            )
        return value


class LogLevelRow(BaseModel):
    """One offered category projected from fresh DB truth + the env basis."""

    category: str
    base_level: str
    ttl_required_below: int
    effective_level: str
    source: str
    override_level: str | None = None
    expires_at: datetime | None = None
    remaining_seconds: int | None = None


class LogLevelsResponse(BaseModel):
    """GET and successful PUT share exactly this envelope."""

    server_time: datetime
    categories: list[LogLevelRow] = Field(default_factory=list)


__all__ = [
    "InvalidLevelSnapshot",
    "LogLevelOverride",
    "LogLevelRow",
    "LogLevelsResponse",
    "LogLevelsUpdate",
    "LoggingDiscoveryUnavailable",
]
