"""Pluggable bindings between the live atoms and their provider contracts.

The live atoms (``SubscriptionActive`` / ``Credits`` / ``OrgRole``) answer
through a sync service contract. ``shared`` must not hard-wire WHICH contract or
HOW its methods are called — that is the provider module's API. An *adapter*
names the contract and translates the atom's question into the provider's call;
an adapter is CONSUMER-side configuration (``register_live_adapter`` in every
process that evaluates the atom — its composition root or the consuming
module's ``register(core)``), or an atom may be handed one explicitly
(``adapter=``). A provider registering an adapter for its own contract is
warned about: in a split deployment the consumer process never sees it.

The built-in defaults keep the current wiring working unchanged:
``billing.entitlements.v1`` (``check_subscription_active`` /
``get_credit_balance``) and ``tenancy.membership.v1`` (``has_role_at_least``).

Contract calls never carry the caller's session (``ctx.db``): the provider
runs each call in its OWN Unit of Work on its own database — identically
in-process and over internal RPC. A custom adapter must not pass one either.

Adapters are stateless code bindings (not runtime state), so a process-level
table is safe across apps/tests; ``reset_live_adapters`` restores defaults.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from uuid import UUID

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.shared.authz.context import AuthzContext

logger = logging.getLogger("sandloom.access.live_contracts")

#: Adapter kinds the shared live atoms consult.
ENTITLEMENTS = "entitlements"
MEMBERSHIP = "membership"


@runtime_checkable
class EntitlementsAdapter(Protocol):
    """Answers subscription / credit questions (``SubscriptionActive``/``Credits``)."""

    service: str

    async def subscription_active(
        self, service: Any, *, tenant_type: str, tenant_id: UUID, ctx: AuthzContext
    ) -> bool: ...

    async def credit_balance(
        self, service: Any, *, tenant_type: str, tenant_id: UUID, ctx: AuthzContext
    ) -> int: ...


@runtime_checkable
class MembershipAdapter(Protocol):
    """Answers organization-role questions for ``OrgRole``."""

    service: str

    async def has_role_at_least(
        self,
        service: Any,
        *,
        org_id: UUID,
        user_id: UUID,
        minimum: str,
        ctx: AuthzContext,
    ) -> bool: ...


class DefaultEntitlementsAdapter:
    """``billing.entitlements.v1`` — the bundled billing module's contract."""

    service = "billing.entitlements.v1"

    async def subscription_active(
        self, service: Any, *, tenant_type: str, tenant_id: UUID, ctx: AuthzContext
    ) -> bool:
        return bool(await service.check_subscription_active(tenant_type, tenant_id))

    async def credit_balance(
        self, service: Any, *, tenant_type: str, tenant_id: UUID, ctx: AuthzContext
    ) -> int:
        return int(await service.get_credit_balance(tenant_type, tenant_id))


class DefaultMembershipAdapter:
    """``tenancy.membership.v1`` — the bundled tenancy module's contract."""

    service = "tenancy.membership.v1"

    async def has_role_at_least(
        self,
        service: Any,
        *,
        org_id: UUID,
        user_id: UUID,
        minimum: str,
        ctx: AuthzContext,
    ) -> bool:
        return bool(await service.has_role_at_least(org_id, user_id, minimum))


def _defaults() -> dict[str, Any]:
    return {
        ENTITLEMENTS: DefaultEntitlementsAdapter(),
        MEMBERSHIP: DefaultMembershipAdapter(),
    }


_ADAPTERS: dict[str, Any] = _defaults()


def _caller_module(depth: int = 2) -> str:
    try:
        frame = sys._getframe(depth)  # diagnostics only
    except ValueError:  # pragma: no cover - shallow stack
        return ""
    return str(frame.f_globals.get("__name__", ""))


def register_live_adapter(kind: str, adapter: Any) -> None:
    """Bind the adapter the live atoms use for ``kind`` — CONSUMER-side config.

    ``kind`` is :data:`ENTITLEMENTS` or :data:`MEMBERSHIP`; ``adapter`` must
    satisfy :class:`EntitlementsAdapter` / :class:`MembershipAdapter`.

    The table is per PROCESS and is read where the atom is EVALUATED — the
    consumer. Register an adapter from the consumer's composition root or the
    consuming module's ``register(core)``, in every process that evaluates
    ``Credits`` / ``SubscriptionActive`` / ``OrgRole``. A provider module that
    registers an adapter for its OWN contract only affects processes that
    load it: in a split deployment the consumer process silently keeps the
    default — so that is logged as a warning.
    """
    expected = {ENTITLEMENTS: EntitlementsAdapter, MEMBERSHIP: MembershipAdapter}
    proto = expected.get(kind)
    if proto is None:
        raise ValueError(
            f"unknown live adapter kind {kind!r}; expected {sorted(expected)}"
        )
    if not isinstance(adapter, proto) or not isinstance(
        getattr(adapter, "service", None), str
    ):
        raise TypeError(f"adapter for {kind!r} must implement {proto.__name__}")
    service_name = str(getattr(adapter, "service", ""))
    owner = service_name.split(".", 1)[0]
    caller = _caller_module()
    if owner and owner in caller.split("."):
        logger.warning(
            "live adapter for %r (%s) registered by its provider module %r: "
            "adapters are consumer-side configuration — a process that "
            "evaluates the atom without loading %r keeps the default adapter. "
            "Register it where the atom is evaluated (consumer / composition "
            "root).",
            kind,
            service_name,
            caller,
            owner,
        )
    _ADAPTERS[kind] = adapter


def get_live_adapter(kind: str) -> Any:
    """The adapter currently bound for ``kind``."""
    return _ADAPTERS[kind]


def reset_live_adapters() -> None:
    """Restore the built-in default adapters (tests)."""
    _ADAPTERS.clear()
    _ADAPTERS.update(_defaults())
