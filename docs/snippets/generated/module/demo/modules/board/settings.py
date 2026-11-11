"""Settings for the board module.

Keep environment aliases prefixed with ``BOARD_``.
Read validated settings at runtime with ``settings_of(BoardSettings)``
from ``sandloom.sdk.module``.
"""

from pydantic import BaseModel, Field


class BoardSettings(BaseModel):
    default_page_size: int = Field(
        default=20, ge=1, le=100, alias="BOARD_DEFAULT_PAGE_SIZE"
    )
