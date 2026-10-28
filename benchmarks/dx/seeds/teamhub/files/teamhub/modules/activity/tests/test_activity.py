"""Tests of the activity module (fixtures: the sandloom.sdk.testing plugin)."""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from teamhub.shared.contracts.tenancy_membership_v1 import TENANCY_MEMBERSHIP_V1

from sandloom.sdk.testing import as_user, deliver, fake_contract

from ..consumers import InvitationSent

URL = "/api/v1/activity/entries"


class AnyMember:
    async def has_role_at_least(
        self, organization_id: UUID, user_id: UUID, minimum: str
    ) -> bool:
        return minimum == "member"


@pytest.fixture(autouse=True)
def members() -> Iterator[None]:
    with fake_contract(TENANCY_MEMBERSHIP_V1, AnyMember()):
        yield


def sent(org: UUID, email: str) -> InvitationSent:
    return InvitationSent(
        invitation_id=uuid4(),
        tenant_id=org,
        email=email,
        token="t",
        expires_at=datetime.now(UTC),
        invited_by=uuid4(),
    )


async def test_a_sent_invitation_appears_once_in_the_feed(client: Any) -> None:
    org = uuid4()
    await deliver(sent(org, "ann@example.com"), times=2)
    _, headers = as_user(tenant_id=org)
    feed = await client.get(URL, headers=headers)
    assert feed.status_code == 200, feed.text
    assert [e["summary"] for e in feed.json()] == ["Invitation sent to ann@example.com"]


async def test_the_feed_is_per_organization(client: Any) -> None:
    await deliver(sent(uuid4(), "a@example.com"))
    _, headers = as_user(tenant_id=uuid4())
    assert (await client.get(URL, headers=headers)).json() == []
