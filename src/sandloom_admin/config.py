"""The admin console's declared settings (ADR-0015)."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from sandloom.sdk.module import settings_of


class AdminSettings(BaseModel):
    admin_console_enabled: bool | None = Field(
        default=None,
        alias="ADMIN_CONSOLE_ENABLED",
        description=(
            "Serve the admin console at /admin/; unset means on outside "
            "production and off in production."
        ),
    )
    admin_console_dist: str | None = Field(
        default=None,
        alias="ADMIN_CONSOLE_DIST",
        description=(
            "Directory of a console you built yourself, served instead of the "
            "packaged one."
        ),
    )
    #: The compose environment of a process that loads the console
    #: (``sandloom add deploy``; ``sandloom new --with admin`` writes the same
    #: lines into the monolith profile).
    deploy: ClassVar[tuple[str, ...]] = (
        "# The admin console is off in production unless switched on here.",
        "ADMIN_CONSOLE_ENABLED: ${ADMIN_CONSOLE_ENABLED:-false}",
    )


def admin_settings() -> AdminSettings:
    """The console's validated settings of the running application."""
    return settings_of(AdminSettings)
