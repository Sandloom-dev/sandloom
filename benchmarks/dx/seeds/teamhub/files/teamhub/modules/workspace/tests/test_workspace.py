"""Tests of the workspace module (fixtures: the sandloom.sdk.testing plugin)."""

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from teamhub.shared.contracts.tenancy_membership_v1 import TENANCY_MEMBERSHIP_V1

from sandloom.sdk.testing import as_user, fake_contract

URL = "/api/v1/workspace/settings"
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


async def test_an_admin_updates_and_members_read(
    client: Any, members: FakeMembership
) -> None:
    org = uuid4()
    admin = login(members, org, "admin")
    member = login(members, org, "member")
    body = {"display_name": "Acme", "timezone": "Europe/Berlin"}
    put = await client.put(URL, json=body, headers=admin)
    assert put.status_code == 200, put.text
    assert (await client.get(URL, headers=member)).json() == body


async def test_members_cannot_change_settings(
    client: Any, members: FakeMembership
) -> None:
    member = login(members, uuid4(), "member")
    put = await client.put(URL, json={"display_name": "x"}, headers=member)
    assert put.status_code == 403


async def test_organizations_are_isolated(client: Any, members: FakeMembership) -> None:
    a = login(members, uuid4(), "admin")
    b = login(members, uuid4(), "admin")
    await client.put(URL, json={"display_name": "A"}, headers=a)
    assert (await client.get(URL, headers=b)).json()["display_name"] == ""
