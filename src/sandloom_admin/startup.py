"""The console's startup check: say how to create the first administrator.

A fresh application has nobody who can sign in to ``/admin/``. Once per
process, while no platform administrator exists, the log names the command.
The question goes to the module the composition's ``claim_authorities``
assigns ``roles`` to, through its reserved ``<owner>.security.v1``
(``platform_admin_exists``): the console names no module. Without such an
owner, a provider that is not deployed here, or one without that method, the
check is skipped quietly (a debug line says it was not checked): it adds a
hint, never a refusal, and runs once at startup, never on a request.
"""

import logging
from collections.abc import Callable

from sandloom.sdk.contracts import service  # sandloom: advanced owner named by the map
from sandloom.sdk.errors import ContractCallError  # sandloom: advanced 503
from sandloom.sdk.module import ModuleAPI

logger = logging.getLogger("sandloom_admin")

#: The claim-map entry whose owner answers who is a platform administrator.
ROLES_KEY = "roles"
#: The reserved operator contract of a claim owner (``<owner>.security.v1``).
SECURITY_SUFFIX = ".security.v1"
METHOD = "platform_admin_exists"

HINT = (
    "No platform administrator yet, so nobody can sign in to /admin/. Create "
    "one: uv run sandloom admin create --email you@example.com (agents and "
    "scripts: add --invite)"
)

_warned = False


async def warn_without_admin(roles_owner: Callable[[], str | None]) -> None:
    """Log :data:`HINT` once per process while no administrator exists.

    ``roles_owner`` is read now (the claim map is installed after the modules
    register): the owner's public name, or ``None`` when the composition
    assigns none."""
    global _warned
    if _warned:
        return
    owner = roles_owner()
    if owner is None:
        logger.debug("first-administrator check not made: no owner of roles")
        return
    provider = service(owner + SECURITY_SUFFIX, optional=True)
    check = None if provider is None else getattr(provider, METHOD, None)
    if check is None:
        logger.debug(
            "first-administrator check not made: %s offers no %s here",
            owner,
            METHOD,
        )
        return
    # Not critical: a failure (no migration yet, an older owner) is logged
    # by the lifespan and the app starts anyway. An unreachable database (the
    # dev services are stopped) is not this check's news: skipped quietly,
    # tried again at the next start. A grant the owner withholds (403) is the
    # operator's choice: not checked.
    try:
        exists = await check()
    except ContractCallError as exc:
        if exc.status_code not in (403, 503):
            raise
        logger.debug("first-administrator check skipped: %s", exc.detail)
        return
    _warned = True
    if not exists:
        logger.warning(HINT)


def setup(core: ModuleAPI) -> None:
    async def check() -> None:
        await warn_without_admin(lambda: core.claim_owner(ROLES_KEY))

    core.add_startup_task(check)


__all__ = ["HINT", "setup", "warn_without_admin"]
