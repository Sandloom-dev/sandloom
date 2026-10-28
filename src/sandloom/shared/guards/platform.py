"""Platform owner admission is distinct from tenant membership and legacy roles."""

from fastapi import Depends, HTTPException

from sandloom.core.di import sensitive_effect
from sandloom.shared.authz.deps import current_context
from sandloom.shared.subject import UserContext


@sensitive_effect("admin")  # ADR-0009 item 6: an admin gate carries its risk
async def require_platform_admin(
    subject: UserContext = Depends(current_context),
) -> None:
    if (
        "admin" not in subject.roles
        or "identity" not in subject.claims_sources
        or "identity" in subject.claims_sources_failed
    ):
        raise HTTPException(403, "Platform administrator role required")
