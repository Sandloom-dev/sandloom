"""Platform owner admission is distinct from tenant membership and legacy roles."""

from fastapi import Depends

from sandloom.core.auth.claims_view import platform_admission
from sandloom.core.di import sensitive_effect
from sandloom.shared.authz.deps import current_context
from sandloom.shared.subject import UserContext


@sensitive_effect("admin")  # ADR-0009 item 6: an admin gate carries its risk
async def require_platform_admin(
    subject: UserContext = Depends(current_context),
) -> None:
    # The one platform admission (ADR-0014 decision 3), shared with the
    # platform console's ``platform_operator``: the role ``admin`` counts
    # only when ``roles`` is in the token's trusted view.
    platform_admission(subject.roles, subject.untrusted_claims)
