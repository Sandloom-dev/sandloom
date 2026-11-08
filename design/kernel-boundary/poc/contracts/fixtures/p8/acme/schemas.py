from pydantic import BaseModel


class Limit(BaseModel):
    """Shared by both versions (one wire shape, one copy)."""

    name: str
    maximum: int


class Quota(BaseModel):
    used: int
    limit: Limit


class QuotaV2(BaseModel):
    used: int
    limits: list[Limit]
    note: str | None = None
