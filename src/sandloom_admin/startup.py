"""The console's startup check: say how to create the first administrator.

A fresh application has nobody who can sign in to ``/admin/``. Once per
process, while no platform administrator exists, the log names the command.
The question goes to the module the composition's ``claim_authorities``
assigns ``roles`` to, through its reserved ``<owner>.security.v1``
(``platform_admin_exists``): the console names no module. Without such an
owner, a provider that is not deployed here, one without that method, one
that withholds this service's grant or cannot be reached, or an answer that
is not the expected shape, the check is skipped quietly (a debug line says
it was not checked): it adds a hint, never a refusal, and runs once at
startup, never on a request.

The consumer's expectation (ADR-0013 decision 4, ``<m>.security.v1``
reserved, not exempt): the owner's name is known only once the claim map is
installed, so no stub or expectation file can be generated for it (the
generator plans copies from names written in the declarations). The console
states the shape it relies on instead, :data:`EXPECTED`, and holds the owner
to it before using an answer: the method takes no argument and answers
``True`` or ``False``. Anything else is the owner's other contract, not this
one, and the hint is not given.
"""

import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sandloom.sdk.contracts import service  # sandloom: advanced owner named by the map
from sandloom.sdk.errors import (  # sandloom: advanced a quiet skip, never a failure
    ContractCallError,
    RemoteServiceUnavailable,
    ServiceNotFound,
    ServiceUnavailableInDeployment,
)
from sandloom.sdk.module import ModuleAPI

logger = logging.getLogger("sandloom_admin")

#: The claim-map entry whose owner answers who is a platform administrator.
ROLES_KEY = "roles"
#: The reserved operator contract of a claim owner (``<owner>.security.v1``).
SECURITY_SUFFIX = ".security.v1"
METHOD = "platform_admin_exists"
#: The console's expectation of the owner's method: its parameters (none) and
#: the type of its answer.
EXPECTED: dict[str, tuple[tuple[str, ...], type]] = {METHOD: ((), bool)}
#: What a provider says when it does not know the method, or withholds this
#: service's grant (403), or cannot serve now (503): not this check's news.
_QUIET_STATUS = (403, 404, 503)

HINT = (
    "No platform administrator yet, so nobody can sign in to /admin/. Create "
    "one: uv run sandloom admin create --email you@example.com (agents and "
    "scripts: add --invite)"
)

_warned = False


def _not_checked(why: str, *args: Any) -> None:
    """The documented quiet line: once, at startup, at debug level."""
    logger.debug("first-administrator check not made: %s", why % args)


def _takes_arguments(check: Any) -> bool:
    """Whether a local method needs an argument :data:`EXPECTED` does not
    pass (a remote one is held to its answer: its signature is the
    provider's)."""
    try:
        signature = inspect.signature(check)
    except (TypeError, ValueError):
        return False
    params, _ = EXPECTED[METHOD]
    return any(
        p.default is p.empty
        and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
        and p.name not in params
        for p in signature.parameters.values()
    )


def _method(contract: str) -> Callable[[], Awaitable[Any]] | None:
    """The owner's method of :data:`EXPECTED`'s shape as deployed here, else
    ``None`` (the quiet line says why)."""
    try:
        provider = service(contract, optional=True)
    except (ServiceNotFound, ServiceUnavailableInDeployment):
        provider = None
    if provider is None:
        _not_checked("%s is not deployed here", contract)
        return None
    check = getattr(provider, METHOD, None)
    if not callable(check) or _takes_arguments(check):
        _not_checked("%s offers no %s()", contract, METHOD)
        return None
    return check  # type: ignore[no-any-return]


async def warn_without_admin(roles_owner: Callable[[], str | None]) -> None:
    """Log :data:`HINT` once per process while no administrator exists.

    ``roles_owner`` is read now (the claim map is installed after the modules
    register): the owner's public name, or ``None`` when the composition
    assigns none or names a module this process cannot vouch for."""
    global _warned
    if _warned:
        return
    owner = roles_owner()
    if owner is None:
        _not_checked("no owner of roles")
        return
    contract = owner + SECURITY_SUFFIX
    check = _method(contract)
    if check is None:
        return
    # Not critical: an unexpected failure (no migration yet) is logged by the
    # lifespan and the app starts anyway. A provider that cannot be reached
    # (the dev services are stopped), withholds this service's grant (403,
    # the operator's choice) or does not know the method (a remote proxy
    # offers every name) is not this check's news: skipped quietly, tried
    # again at the next start.
    try:
        exists = await check()
    except RemoteServiceUnavailable as exc:
        _not_checked("%s: %s", contract, exc.detail)
        return
    except ContractCallError as exc:
        if exc.status_code not in _QUIET_STATUS:
            raise
        _not_checked("%s: %s", contract, exc.detail)
        return
    _, answer = EXPECTED[METHOD]
    if not isinstance(exists, answer):
        _not_checked(
            "%s.%s answered %s, not %s",
            contract,
            METHOD,
            type(exists).__name__,
            answer.__name__,
        )
        return
    _warned = True
    if not exists:
        logger.warning(HINT)


def setup(core: ModuleAPI) -> None:
    async def check() -> None:
        await warn_without_admin(lambda: core.claim_owner(ROLES_KEY))

    core.add_startup_task(check)


__all__ = ["EXPECTED", "HINT", "setup", "warn_without_admin"]
