"""Request and response shapes (pydantic only)."""

from pydantic import BaseModel, ConfigDict, Field


class SettingsIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    timezone: str = Field(default="UTC", min_length=1, max_length=64)


class SettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    display_name: str
    timezone: str
