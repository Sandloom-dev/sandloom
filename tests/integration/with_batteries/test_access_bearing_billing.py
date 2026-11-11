"""A provider-only process classifies billing's contract (ADR-0014 decision 6, L1).

No consumer imported a generated atom of ``billing.entitlements.v1`` and the
root assigns nothing: billing's implementation carries ``@access_check``
methods, so the name is access-bearing anyway, and a module other than
billing cannot first-register it, before or after billing.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI

from sandloom.core.app_factory import access_providers as _access
from sandloom.core.config.common import ConfigurationError
from sandloom.core.facade import CoreFacade
from sandloom.core.facade._services import ForeignOwnerRegistration
from sandloom.core.module_api import contract_method
from sandloom.core.module_api._registration import (
    ModuleRegistrationConflict,
)
from sandloom.shared.authz import contract_check as _contract_check
from sandloom_billing.contracts import EntitlementsContract

LOG = logging.getLogger("test.access_bearing")
ENTITLEMENTS = "billing.entitlements.v1"


class _Plain:
    """An implementation of billing's names with no access check."""

    @classmethod
    @contract_method
    async def check_subscription_active(cls, tenant_type: str, tenant_id: str) -> bool:
        """Answer anything."""
        return True


@pytest.fixture(autouse=True)
def _provider_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider-only process: no consumer's generated atom is imported
    (another test of the session may have imported one)."""
    monkeypatch.setattr(_contract_check, "generated_access_contracts", frozenset)


def _core() -> CoreFacade:
    core = CoreFacade(app=FastAPI())
    _access.install_access_providers(core, access_providers=None, log=LOG)
    return core


def _register(core: CoreFacade, module: str, impl: object) -> None:
    with core.module_registration(module) as api:
        api.register_service(ENTITLEMENTS, impl)


def test_a_provider_only_process_refuses_a_foreign_first_registrant() -> None:
    """``acme_notes`` serving billing's implementation is refused."""
    core = _core()
    with pytest.raises(ForeignOwnerRegistration, match="SANDLOOM-B034"):
        _register(core, "acme_notes", EntitlementsContract)
    assert not core.service_registry.contains(ENTITLEMENTS)
    _register(core, "sandloom_billing", EntitlementsContract)
    assert core.contract_owner(ENTITLEMENTS) == "sandloom_billing"


def test_a_plain_first_registrant_is_refused_once_billing_offers_its_own() -> None:
    """Registered first without an access check, the foreign module keeps
    the name only until billing's refused attempt classifies it: the
    post-load sweep refuses the boot."""
    core = _core()
    _register(core, "acme_notes", _Plain)
    with pytest.raises(ModuleRegistrationConflict):
        _register(core, "sandloom_billing", EntitlementsContract)
    with pytest.raises(ConfigurationError, match="SANDLOOM-B034") as exc:
        _access.enforce_access_providers(core, log=LOG)
    assert "'acme_notes'" in str(exc.value)
