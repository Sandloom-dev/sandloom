"""KB-49 fixture (P15): reads its settings at import time — must fail the audit."""

from pydantic import BaseModel, Field

from sandloom.sdk import define_module
from sandloom.sdk.module import settings_of


class BadSettings(BaseModel):
    x: int = Field(default=1, alias="INVENTORY_BAD_X")


VALUE = settings_of(BadSettings).x  # module-level read

register = define_module(__name__, version="0.1.0", settings=BadSettings, router=None)
