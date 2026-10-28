from datetime import datetime, timezone
from typing import Optional
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator
from app.validators import EscapedStr, PlainStr


def _normalize_expires_at_naive_utc(v: Optional[datetime]) -> Optional[datetime]:
    """WR-06 — normalize tz-aware expires_at input to naive UTC.

    The auth-time expiry compares (`expires_at < datetime.now(timezone.utc).replace(tzinfo=None)` in
    get_public_api_consumer, both DB and cache-hit paths) compare against a
    naive UTC timestamp; a tz-aware value would raise TypeError on the cache-hit
    path. Convert aware input to UTC then drop tzinfo; naive values and None
    pass through unchanged.
    """
    if v is not None and v.tzinfo is not None:
        return v.astimezone(timezone.utc).replace(tzinfo=None)
    return v


class ApiClientBase(BaseModel):
    name: EscapedStr = Field(max_length=255)
    expires_at: Optional[datetime] = None

    @field_validator("expires_at")
    @classmethod
    def _expires_at_naive_utc(cls, v):
        return _normalize_expires_at_naive_utc(v)


class ApiClientCreate(ApiClientBase):
    pass


class ApiClientUpdate(BaseModel):
    name: Optional[EscapedStr] = Field(default=None, max_length=255)
    expires_at: Optional[datetime] = None

    @field_validator("expires_at")
    @classmethod
    def _expires_at_naive_utc(cls, v):
        return _normalize_expires_at_naive_utc(v)


class ApiClientResponse(ApiClientBase):
    id: UUID
    token_prefix: PlainStr = Field(max_length=16)
    revoked_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ApiClientCreateResponse(ApiClientResponse):
    token: PlainStr = Field(max_length=64)
