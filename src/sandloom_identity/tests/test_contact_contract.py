"""identity.contact.v1: how other modules reach a user (events carry no PII)."""

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
from sandloom_identity.models.user import User
from sandloom_identity.schemas.contact import UserContact


async def _user(db: AsyncSession, *, verified: bool = True) -> User:
    user = User(
        email=f"contact-{uuid.uuid4().hex[:8]}@example.com",
        full_name="Ada Contact",
        hashed_password=None,
        is_verified=verified,
    )
    db.add(user)
    await db.flush()
    return user


async def test_get_contact_and_get_contacts(
    sandloom_core: Any, test_db: AsyncSession
) -> None:
    known = await _user(test_db)
    other = await _user(test_db, verified=False)
    contact = sandloom_core.get_service("identity.contact.v1")
    with bind_contract_session(test_db):
        found = await contact.get_contact(known.id)
        assert found == UserContact(
            user_id=known.id,
            email=known.email,
            full_name="Ada Contact",
            email_verified=True,
            is_active=True,
        )
        assert await contact.get_contact(uuid.uuid4()) is None
        many = await contact.get_contacts([other.id, uuid.uuid4(), known.id])
    assert [c.user_id for c in many] == sorted([known.id, other.id])
    assert {c.user_id: c.email_verified for c in many}[other.id] is False


@pytest.fixture
def _internal_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from sandloom.sdk.infra import settings

    monkeypatch.setattr(settings, "internal_api_token", "conformance-secret")


@pytest.mark.usefixtures("_internal_token")
async def test_contact_contract_conforms(
    sandloom_core: Any, test_db: AsyncSession
) -> None:
    user = await _user(test_db)
    with bind_contract_session(test_db):
        await assert_contract_conformance(
            sandloom_core,
            "identity.contact.v1",
            [
                ContractCase("get_contact", (user.id,)),
                ContractCase("get_contact", (uuid.uuid4(),), label="unknown"),
                ContractCase("get_contacts", ([user.id],)),
            ],
        )
