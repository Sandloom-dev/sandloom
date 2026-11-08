"""Security/ops hardening: settings, JWT, HTTP error mapping, health, logging.

Moved from ``sandloom/core/tests/test_ops_security_hardening.py`` (KB-05): these tests need
the bundled modules, so they run where the batteries are installed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from sandloom.core.app_factory.claim_authorities import build_claim_authorities
from sandloom.core.facade import CoreFacade


@pytest.mark.asyncio
async def test_claims_provenance_from_registering_module() -> None:
    core = CoreFacade(app=FastAPI())
    # A protected claim needs its assigned authority (ADR-0014).
    core.assign_claim_authorities(
        build_claim_authorities({"subscription_status": "myapp.modules.billing"})
    )

    async def billing_claims(user_id: Any) -> dict[str, Any]:
        return {"subscription_status": "active"}

    async def broken(user_id: Any) -> dict[str, Any]:
        raise RuntimeError("down")

    async def empty(user_id: Any) -> dict[str, Any]:
        return {}

    # A downstream app module (not under sandloom.*) gets provenance too.
    with core.module_registration("myapp.modules.billing") as api:
        api.add_claims_contributor(billing_claims)
    with core.module_registration("myapp.modules.reports") as api:
        api.add_claims_contributor(broken)
    with core.module_registration("sandloom_identity") as api:
        api.add_claims_contributor(empty)

    # Contributors run through the contract runtime; a bare facade has no
    # database, so bind a stand-in session for them (the test seam).
    from sandloom.core.module_api import bind_contract_session

    with bind_contract_session(SimpleNamespace()):
        claims = await core.collect_claims("u")
    assert claims["subscription_status"] == "active"
    # identity ran (even with no claims) -> authoritative; reports failed.
    assert claims["claims_sources"] == ["billing", "identity"]
    assert claims["claims_sources_failed"] == ["reports"]
