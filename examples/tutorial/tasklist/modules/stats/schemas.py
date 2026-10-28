"""Request and response shapes (pydantic only)."""

from pydantic import BaseModel


class CountOut(BaseModel):
    items: int
