"""KB-49 fixture: declares an env name inventory already declares (P2 collision)."""

from pydantic import BaseModel, Field

from sandloom.sdk import define_module


class DupSettings(BaseModel):
    ttl: int = Field(default=1, alias="INVENTORY_RESERVE_TTL_SECONDS")


register = define_module(__name__, version="0.1.0", settings=DupSettings, router=None)
