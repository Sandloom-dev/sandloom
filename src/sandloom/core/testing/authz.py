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
restored afterwards. A policy that names the fact's owner
(``Entitled(name, owner="billing")``) accepts only that owner's fact: fake it
with ``owner="billing"``.

It answers a fact by name and nothing else: a generated atom (``OrgRole``,
``SubscriptionActive``, ``Credits``, ``Subscribed``, ...) asks its provider's
contract, so a test serves that contract with ``fake_contract(REF, Fake())``.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import sandloom.core.runtime as _runtime

#: The ``owner_module`` of a faked fact when the test names no owner.
_FAKE_OWNER = "sandloom.core.testing"


@dataclass(frozen=True)
class _FakeFact:
    """Duck-typed ``AccessFact`` resolved by its projection path."""

    name: str
    value: bool | Callable[[Any], Any]
    deny_category: str | None = None
    owner_module: str = _FAKE_OWNER
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
    owner: str | None = None,
) -> Iterator[None]:
    """Answer the access fact ``name`` with ``value`` inside the block
    (registered under ``owner`` when the policy names one)."""
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
    facts[name] = _FakeFact(name, value, deny_category, owner or _FAKE_OWNER)
    try:
        yield
    finally:
        if previous is missing:
            facts.pop(name, None)
        else:
            facts[name] = previous


__all__ = ["fake_access_fact"]
