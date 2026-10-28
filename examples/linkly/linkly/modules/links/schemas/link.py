"""Link DTOs (API contracts are ORM-free per architecture rules)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

_CODE_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


class LinkCreateIn(BaseModel):
    target_url: str = Field(min_length=1, max_length=2048)
    custom_code: str | None = Field(default=None, min_length=3, max_length=16)

    @field_validator("target_url")
    @classmethod
    def _must_be_http(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("target_url must start with http:// or https://")
        return v

    @field_validator("custom_code")
    @classmethod
    def _code_charset(cls, v: str | None) -> str | None:
        if v is not None and any(c not in _CODE_ALPHABET for c in v):
            raise ValueError("custom_code may contain only [A-Za-z0-9]")
        return v


class LinkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    target_url: str
    clicks: int
    created_at: datetime


class LinkTargetOut(BaseModel):
    """A short code's redirect target (the declared read of ``resolve``)."""

    target_url: str


class LinkStatsOut(BaseModel):
    code: str
    clicks: int
