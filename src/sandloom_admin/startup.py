"""The console's startup check: say how to create the first administrator.

A fresh application has nobody who can sign in to ``/admin/``. Once per
process, while no platform administrator exists, the log names the command.
Identity is asked through ``identity.security.v1`` (optional: a service
without identity says nothing).
"""

import logging

from sandloom.sdk import Contract
from sandloom.sdk.errors import ContractCallError  # sandloom: advanced 503
from sandloom.sdk.module import ModuleAPI
from sandloom_admin._generated.contracts.identity_security_v1 import (
    IDENTITY_SECURITY_V1,
)

logger = logging.getLogger("sandloom_admin")

security = Contract(IDENTITY_SECURITY_V1, optional=True)

HINT = (
    "No platform administrator yet, so nobody can sign in to /admin/. Create "
    "one: uv run sandloom admin create --email you@example.com (agents and "
    "scripts: add --invite)"
)

_warned = False


async def warn_without_admin() -> None:
    """Log :data:`HINT` once per process while no administrator exists."""
    global _warned
    if _warned:
        return
    client = security.client()
    if client is None:
        return
    # Not critical: a failure (no migration yet, an older identity) is logged
    # by the lifespan and the app starts anyway. An unreachable database (the
    # dev services are stopped) is not this check's news: skipped quietly,
    # tried again at the next start.
    try:
        exists = await client.platform_admin_exists()
    except ContractCallError as exc:
        if exc.status_code != 503:
            raise
        logger.debug("first-administrator check skipped: %s", exc.detail)
        return
    _warned = True
    if not exists:
        logger.warning(HINT)


def setup(core: ModuleAPI) -> None:
    core.add_startup_task(warn_without_admin)


__all__ = ["HINT", "setup", "warn_without_admin"]
