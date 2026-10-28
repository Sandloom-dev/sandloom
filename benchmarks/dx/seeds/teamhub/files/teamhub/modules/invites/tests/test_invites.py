"""Tests of the invites module (fixtures: the sandloom.sdk.testing plugin)."""

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from teamhub.shared.contracts.tenancy_membership_v1 import TENANCY_MEMBERSHIP_V1

from sandloom.sdk.testing import as_user, fake_contract, published

from ..events import InvitationSent
from ..services import hash_token

URL = "/api/v1/invites/invitations"
_LADDER = {"member": 1, "admin": 2, "owner": 3}


class FakeMembership:
    """Roles by (organization, user); the real contract is tenancy's."""

    def __init__(self) -> None:
        self.roles: dict[tuple[UUID, UUID], str] = {}

    async def has_role_at_least(
        self, organization_id: UUID, user_id: UUID, minimum: str
    ) -> bool:
        role = self.roles.get((organization_id, user_id))
        return role is not None and _LADDER[role] >= _LADDER[minimum]


@pytest.fixture
def members() -> Iterator[FakeMembership]:
    fake = FakeMembership()
    with fake_contract(TENANCY_MEMBERSHIP_V1, fake):
        yield fake


def login(members: FakeMembership, org: UUID, role: str) -> dict[str, str]:
    user_id, headers = as_user(tenant_id=org)
    members.roles[(org, user_id)] = role
    return headers


async def test_a_member_sends_and_lists_invitations(
    client: Any, members: FakeMembership
) -> None:
    org = uuid4()
    headers = login(members, org, "member")
    sent = await client.post(URL, json={"email": "Ann@Example.com"}, headers=headers)
    assert sent.status_code == 201, sent.text
    assert sent.json()["status"] == "pending"
    [event] = published(InvitationSent)
    assert event.email == "ann@example.com"
    assert event.tenant_id == org
    assert hash_token(event.token) != event.token
    listed = await client.get(URL, headers=headers)
    assert [i["email"] for i in listed.json()] == ["ann@example.com"]


async def test_organizations_do_not_see_each_others_invitations(
    client: Any, members: FakeMembership
) -> None:
    a = login(members, uuid4(), "member")
    b = login(members, uuid4(), "member")
    sent = await client.post(URL, json={"email": "a@example.com"}, headers=a)
    assert sent.status_code == 201, sent.text
    assert (await client.get(URL, headers=b)).json() == []


async def test_outsiders_cannot_invite(client: Any, members: FakeMembership) -> None:
    _, headers = as_user(tenant_id=uuid4())  # no membership
    sent = await client.post(URL, json={"email": "x@example.com"}, headers=headers)
    assert sent.status_code == 403
    assert (await client.post(URL, json={"email": "x@example.com"})).status_code == 401
