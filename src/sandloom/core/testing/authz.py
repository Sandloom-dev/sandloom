"""Authorization seams for tests: fake an access fact (an entitlement).

A module gating on ``Entitled("subscription.active")`` (or any fact another
module owns) needs that owner to resolve it — billing, in practice. A module
test must not import billing; :func:`fake_access_fact` answers the fact on the
active application for the duration of a block::

    with fake_access_fact("subscription.active", True):
        assert (await client.post(...)).status_code == 201
    with fake_access_fact(
        "subscription.active", False, deny_category="payment_required"
    ):
        assert (await client.post(...)).status_code == 402
    with fake_access_fact("quota.ok", lambda ctx: ctx.subject.user_id == alice):
        ...

``value`` is a bool, or a callable of the authz context returning one (sync
or async; it may raise ``FactDenied``). The previous registration, if any, is
restored afterwards.

The two subscription facts also drive the live billing atoms that ask the
same question, so a route gated either way is faked the same way:

- ``"subscription.active"``: ``SubscriptionActive(of=Account.user())``;
- ``"organization.subscription.active"``: ``SubscriptionActive`` /
  ``Subscribed`` with ``of=Account.org(...)`` (or a bound organization).

The live entitlements adapter answers the faked tenant kind from ``value``
and the other one (and ``Credits``) from the real provider; when billing is
not loaded, the entitlements contract resolves to a stub that only the faked
question may use.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import sandloom.core.runtime as _runtime
from sandloom.core.module_api._service_registry import ServiceNotFound
from sandloom.core.testing.services import override_service
from sandloom.shared.authz import live_contracts as _live

#: Fact name -> the tenant kind of the live entitlement atoms that ask it.
LIVE_SUBSCRIPTION_FACTS = {
    "subscription.active": "user",
    "organization.subscription.active": "organization",
}


@dataclass(frozen=True)
class _FakeFact:
    """Duck-typed ``AccessFact`` resolved by its projection path."""

    name: str
    value: bool | Callable[[Any], Any]
    deny_category: str | None = None
    owner_module: str = "sandloom.core.testing"
    claim_key: str | None = None
    claim_rule: Any = None
    live_service: str | None = None
    live_resolver: Any = None
    min_freshness: str = "claim"
    default_mode: str = "projection"

    async def projection_reader(self, ctx: Any) -> bool:
        if not callable(self.value):
            return bool(self.value)
        result = self.value(ctx)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

    def current_claim_rule(self) -> Any:
        return None

    def interpret_claim(self, value: Any) -> bool:
        return bool(value)


@contextmanager
def fake_access_fact(
    name: str,
    value: bool | Callable[[Any], Any] = True,
    *,
    deny_category: str | None = None,
    core: Any = None,
) -> Iterator[None]:
    """Answer the access fact ``name`` with ``value`` inside the block."""
    if core is None:
        core = _runtime.get_active_core()
    if core is None:
        raise RuntimeError(
            "fake_access_fact() needs the test application (no active core): "
            "request the `client` (or `sandloom_app`) fixture in this test, or "
            "pass core="
        )
    facts: dict[str, Any] = core._access_facts
    missing = object()
    previous = facts.get(name, missing)
    fake = _FakeFact(name, value, deny_category)
    facts[name] = fake
    try:
        tenant_type = LIVE_SUBSCRIPTION_FACTS.get(name)
        if tenant_type is None:
            yield
        else:
            with _fake_live_entitlements(core, tenant_type, fake):
                yield
    finally:
        if previous is missing:
            facts.pop(name, None)
        else:
            facts[name] = previous


class _StubEntitlements:
    """``billing.entitlements.v1`` while billing is not loaded: only the
    faked subscription question is answered (by the fake adapter)."""

    def __getattr__(self, method: str) -> Any:
        raise RuntimeError(
            f"billing.entitlements.v1.{method}: billing is not loaded and "
            "fake_access_fact fakes only the subscription question; fake the "
            "contract with fake_contract(...) for anything else"
        )


@dataclass
class _FakeEntitlementsAdapter:
    inner: Any
    tenant_type: str
    fake: _FakeFact

    @property
    def service(self) -> str:
        return str(self.inner.service)

    async def subscription_active(
        self, service: Any, *, tenant_type: str, tenant_id: Any, ctx: Any
    ) -> bool:
        if tenant_type == self.tenant_type:
            return await self.fake.projection_reader(ctx)
        return bool(
            await self.inner.subscription_active(
                service, tenant_type=tenant_type, tenant_id=tenant_id, ctx=ctx
            )
        )

    async def credit_balance(
        self, service: Any, *, tenant_type: str, tenant_id: Any, ctx: Any
    ) -> int:
        return int(
            await self.inner.credit_balance(
                service, tenant_type=tenant_type, tenant_id=tenant_id, ctx=ctx
            )
        )


@contextmanager
def _fake_live_entitlements(
    core: Any, tenant_type: str, fake: _FakeFact
) -> Iterator[None]:
    inner = _live.get_live_adapter(_live.ENTITLEMENTS)
    _live.register_live_adapter(
        _live.ENTITLEMENTS, _FakeEntitlementsAdapter(inner, tenant_type, fake)
    )
    try:
        try:
            core.get_service(inner.service)
        except ServiceNotFound:
            with override_service(core, inner.service, _StubEntitlements()):
                yield
        else:
            yield
    finally:
        _live.register_live_adapter(_live.ENTITLEMENTS, inner)


__all__ = ["LIVE_SUBSCRIPTION_FACTS", "fake_access_fact"]
