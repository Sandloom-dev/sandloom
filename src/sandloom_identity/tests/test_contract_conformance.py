"""identity.claims.v1 behaves identically local vs RPC."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk.testing import (
    ContractCase,
    assert_contract_conformance,
    bind_contract_session,
)


@pytest.fixture(autouse=True)
def _internal_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from sandloom.sdk.infra import settings

    monkeypatch.setattr(settings, "internal_api_token", "conformance-secret")


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["asgi", "aiohttp", "httpx"])
async def test_identity_claims_contract_conforms(
    sandloom_core: Any, test_db: AsyncSession, transport: str
) -> None:
    core = sandloom_core

    with bind_contract_session(test_db):
        await assert_contract_conformance(
            core,
            "identity.claims.v1",
            [
                ContractCase("contribute", (uuid.uuid4(),)),
                ContractCase("contribute", (), label="missing user id"),
            ],
            transport=transport,
        )
