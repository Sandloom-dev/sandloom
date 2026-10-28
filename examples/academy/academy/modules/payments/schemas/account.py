"""Payments API DTOs (persistence-free)."""

from uuid import UUID

from pydantic import BaseModel, Field


class TopupIn(BaseModel):
    amount: int = Field(gt=0, le=1_000_000)


class BalanceOut(BaseModel):
    customer_id: UUID
    balance: int
