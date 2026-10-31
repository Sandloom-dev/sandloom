"""Request and response shapes of the profiles module."""

from pydantic import BaseModel, Field


class ProfileIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)


class ProfileOut(BaseModel):
    display_name: str
