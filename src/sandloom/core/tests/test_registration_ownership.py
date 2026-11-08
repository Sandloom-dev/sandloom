"""A module registers claims, facts and their contracts only under its own name.

Before SANDLOOM-B034 the kernel took the owner a registration named: a module
``forged`` could register an access fact with ``owner_module="inventory"`` and
become the owner of ``inventory.facts.v1`` (validation C4), register
``identity.claims.v1`` itself (R2), or pass ``owner="identity"`` to
``CoreFacade.add_claims_contributor`` and be stamped ``identity`` in
``claims_sources`` (R3). Each of these is now refused at registration, as is
any of them made outside a module's registration; over RPC a consumer drops a
fact a ``<m>.facts.v1`` provider describes under another module.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi import FastAPI

from sandloom.core.error_codes import code_for_exception
from sandloom.core.facade import CoreFacade
from sandloom.core.facade._services import ForeignOwnerRegistration
from sandloom.core.module_api import contract_method
from sandloom.shared.authz.facts import AccessFact, _describe_owner


def _fact(name: str, owner: str) -> AccessFact:
    """A claim-mode fact ``name`` owned by ``owner``."""
    return AccessFact(name=name, owner_module=owner, claim_key="stock")


async def _claims(user_id: Any) -> dict[str, Any]:
    """A contributor with nothing to say."""
    return {}


class _Stock:
    """A provider standing in for a reserved contract."""

    @classmethod
    @contract_method
    async def ping(cls) -> int:
        """Answer anything."""
        return 0


@contextmanager
def _applying(core: CoreFacade, module: str) -> Iterator[None]:
    """The kernel's state while ``module``'s draft is applied (as the loader does)."""
    previous = core._registering_module
    core._registering_module = module
    try:
        yield
    finally:
        core._registering_module = previous


def test_a_module_registers_its_own_fact_and_facts_contract() -> None:
    """The ordinary case is unchanged: the owner is the registering module."""
    core = CoreFacade(app=FastAPI())
    with core.module_registration("shop.modules.inventory") as api:
        api.register_access_fact(_fact("inventory.stock.available", "inventory"))
    assert core.get_access_fact("inventory.stock.available") is not None
    assert core.contract_owner("inventory.facts.v1") == "shop.modules.inventory"


def test_a_fact_naming_another_owner_refuses_the_load() -> None:
    """Validation C4: ``forged`` cannot answer ``inventory``'s fact."""
    core = CoreFacade(app=FastAPI())
    with (
        pytest.raises(ForeignOwnerRegistration, match="SANDLOOM-B034") as exc,
        core.module_registration("shop.modules.forged") as api,
    ):
        api.register_access_fact(_fact("inventory.stock.available", "inventory"))
    assert code_for_exception(exc.value) == "SANDLOOM-B034"
    assert "'shop.modules.forged'" in str(exc.value)
    assert "'inventory'" in str(exc.value)
    # nothing of the module was applied
    assert core.get_access_fact("inventory.stock.available") is None
    assert core.contract_owner("inventory.facts.v1") is None
    assert not core.service_registry.contains("inventory.facts.v1")


@pytest.mark.parametrize("contract", ["identity.claims.v1", "inventory.facts.v1"])
def test_another_modules_reserved_contract_refuses_the_load(contract: str) -> None:
    """R2: a module cannot register another module's claims or facts contract."""
    core = CoreFacade(app=FastAPI())
    with (
        pytest.raises(ForeignOwnerRegistration, match="SANDLOOM-B034"),
        core.module_registration("shop.modules.forged") as api,
    ):
        api.register_service(contract, _Stock)
    assert not core.service_registry.contains(contract)
    assert core.contract_owner(contract) is None


def test_a_module_may_register_its_own_reserved_contract_name() -> None:
    """Only the name check applies: ``forged.facts.v1`` belongs to ``forged``."""
    core = CoreFacade(app=FastAPI())
    with core.module_registration("shop.modules.forged") as api:
        api.register_service("forged.facts.v1", _Stock)
    assert core.contract_owner("forged.facts.v1") == "shop.modules.forged"


def test_owner_naming_another_module_is_refused() -> None:
    """R3: ``owner="identity"`` from module ``inventory`` is not stamped."""
    core = CoreFacade(app=FastAPI())
    with (
        _applying(core, "shop.modules.inventory"),
        pytest.raises(ForeignOwnerRegistration, match="SANDLOOM-B034"),
    ):
        core.add_claims_contributor(_claims, owner="identity")
    assert core.claims_contributors == []
    assert not core.service_registry.contains("identity.claims.v1")


def test_owner_naming_the_registering_module_is_accepted() -> None:
    """``owner=`` equal to the registering module changes nothing."""
    core = CoreFacade(app=FastAPI())
    with _applying(core, "shop.modules.inventory"):
        core.add_claims_contributor(_claims, owner="inventory")
    assert core._local_claim_owners() == {"inventory": False}
    assert core.service_registry.contains("inventory.claims.v1")


def test_a_spoofed_module_name_gets_its_own_name_only() -> None:
    """A module named after another (``app.modules.identity_x``) owns only its name."""
    core = CoreFacade(app=FastAPI())
    with core.module_registration("shop.modules.identity_x") as api:
        api.add_claims_contributor(_claims, critical=True)
    assert core._local_claim_owners() == {"identity_x": True}
    assert not core.service_registry.contains("identity.claims.v1")


def test_registrations_outside_a_module_context_are_refused() -> None:
    """A direct call on the core (a startup task, the composition root) has no
    module to check the name against, so it is refused."""
    core = CoreFacade(app=FastAPI())
    with pytest.raises(ForeignOwnerRegistration, match="outside a module"):
        core.register_access_fact(_fact("inventory.stock.available", "inventory"))
    with pytest.raises(ForeignOwnerRegistration, match="outside a module"):
        core.add_claims_contributor(_claims)
    with pytest.raises(ForeignOwnerRegistration, match="outside a module"):
        core.add_claims_contributor(_claims, owner="identity")
    with pytest.raises(ForeignOwnerRegistration, match="outside a module"):
        core.register_service("identity.claims.v1", _Stock)
    assert core.get_access_fact("inventory.stock.available") is None
    assert core.claims_contributors == []


def test_a_direct_core_call_during_register_is_outside_the_context() -> None:
    """``register()`` writes only through its ``ModuleAPI``: the core's own
    methods called while it runs see no registering module."""
    core = CoreFacade(app=FastAPI())
    with (
        pytest.raises(ForeignOwnerRegistration, match="outside a module"),
        core.module_registration("shop.modules.inventory"),
    ):
        core.register_access_fact(_fact("inventory.stock.available", "inventory"))


class _Describes:
    """A remote ``<m>.facts.v1`` client answering ``describe``."""

    def __init__(self, descriptors: list[dict[str, Any]]) -> None:
        """Remember what the provider describes."""
        self._descriptors = descriptors

    async def describe(self) -> list[dict[str, Any]]:
        """Answer like ``<m>.facts.v1.describe`` over RPC."""
        return self._descriptors


class _RemoteCore:
    """The consumer's core: ``<m>.facts.v1`` resolves to the remote client."""

    def __init__(self, client: _Describes) -> None:
        """Serve ``client`` for every contract."""
        self._client = client

    def get_service(self, name: str) -> _Describes:
        """The bound remote client."""
        return self._client


@pytest.mark.asyncio
async def test_split_drops_a_fact_described_under_another_owner(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Split: the binding names the owner; a provider bound as ``forged.facts.v1``
    that describes a fact under ``inventory`` is not believed."""
    client = _Describes(
        [
            _fact("inventory.stock.available", "inventory").to_wire(),
            _fact("forged.ok", "forged").to_wire(),
        ]
    )
    with caplog.at_level(logging.WARNING, logger="sandloom.access.facts"):
        facts = await _describe_owner(_RemoteCore(client), "forged")
    assert set(facts) == {"forged.ok"}
    assert facts["forged.ok"].owner_module == "forged"
    assert "SANDLOOM-B034" in caplog.text
    assert "inventory.stock.available" in caplog.text
