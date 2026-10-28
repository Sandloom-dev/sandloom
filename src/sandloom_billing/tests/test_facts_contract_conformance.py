"""billing.facts.v1 (auto-exposed by billing's access facts): local == RPC."""

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


def _subject(user_id: uuid.UUID, **extra: object) -> dict[str, object]:
    return {
        "user_id": str(user_id),
        "roles": [],
        "permissions": [],
        "claims": {},
        "claims_sources": ["identity"],
        "claims_sources_failed": ["billing"],
        "provenance_authoritative": True,
        **extra,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["asgi", "aiohttp", "httpx"])
async def test_billing_facts_contract_conforms(
    sandloom_core: Any, test_db: AsyncSession, transport: str
) -> None:
    core = sandloom_core

    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    with bind_contract_session(test_db):
        await assert_contract_conformance(
            core,
            "billing.facts.v1",
            [
                ContractCase("describe", ()),
                ContractCase(
                    "evaluate",
                    ("subscription.active", _subject(user_id), None),
                    label="personal subscription, live -> false",
                ),
                ContractCase(
                    "evaluate",
                    (
                        "organization.subscription.active",
                        _subject(user_id),
                        {"path_params": {}, "query_params": {}, "headers": {}},
                    ),
                    label="no organization -> denied",
                ),
                ContractCase(
                    "evaluate",
                    (
                        "organization.subscription.active",
                        _subject(user_id),
                        {
                            "path_params": {"org_id": str(org_id)},
                            "query_params": {},
                            "headers": {},
                        },
                    ),
                    label="not a member -> denied",
                ),
                ContractCase(
                    "evaluate",
                    ("nope.fact", _subject(user_id), None),
                    label="unknown fact -> unavailable",
                ),
            ],
            transport=transport,
        )
