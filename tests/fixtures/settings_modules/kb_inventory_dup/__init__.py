"""Declares an environment name kb_inventory owns (SANDLOOM-B036)."""

from pydantic import BaseModel, Field

from sandloom.sdk import define_module


class DupSettings(BaseModel):
    reserve_ttl_seconds: int = Field(60, alias="KB_INVENTORY_RESERVE_TTL_SECONDS")


register = define_module(__name__, version="0.1.0", settings=DupSettings, router=None)
