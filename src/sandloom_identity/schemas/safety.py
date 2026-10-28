"""Emergency account-safety commands (platform operators)."""

from pydantic import BaseModel, ConfigDict, Field


class SafetyReason(BaseModel):
    """Why an operator acts on an account; stored in the audit journal."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=3, max_length=500)


class BlockStatusOut(BaseModel):
    """Live state of the kernel's emergency block mark for one user."""

    id: str
    blocked: bool
