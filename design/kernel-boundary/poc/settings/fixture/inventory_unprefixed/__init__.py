"""KB-49 fixture (P9): a new setting without the module's prefix."""

from pydantic import BaseModel, Field

from sandloom.sdk import define_module


class UnprefixedSettings(BaseModel):
    limit: int = Field(default=1, alias="RESERVE_LIMIT")


register = define_module(__name__, version="0.1.0", settings=UnprefixedSettings, router=None)
