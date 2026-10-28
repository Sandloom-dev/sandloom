"""Member lifecycle events: every change is announced, and ordered by version.

Event delivery is at-least-once with NO ordering guarantee (AGENTS.md), so a
consumer / projection of the membership needs (a) an event for EVERY way a
member appears — including accepting an invitation — and (b) a per-membership
``version`` that increases monotonically across the whole lifecycle of the
``(organization_id, user_id)`` pair, surviving removal and re-adding, so it
can apply events with ``monotonic_upsert`` and never roll a row back.
"""

from typing import Any

import pytest
from httpx import AsyncClient

from sandloom_tenancy import events
from sandloom_tenancy.models import ROLE_ADMIN, ROLE_OWNER
from sandloom_tenancy.tests.conftest import email_of

_MEMBER_CHANNELS = (
    events.MEMBER_ADDED_V1,
    events.MEMBER_ROLE_CHANGED_V1,
    events.MEMBER_REMOVED_V1,
)


async def _invite_and_accept(
    client: AsyncClient, org_id: str, owner_headers: dict, user: Any, role: str
) -> None:
    user_id, headers = user
    invite = await client.post(
        f"/api/v1/orgs/{org_id}/invitations",
        json={"email": email_of(user_id), "role": role},
        headers=owner_headers,
    )
    assert invite.status_code == 201, invite.text
    accept = await client.post(
        "/api/v1/invitations/accept",
        json={"token": invite.json()["token"]},
        headers=headers,
    )
    assert accept.status_code == 200, accept.text


def _member_events(captured: Any, user_id: Any) -> list[tuple[str, dict[str, Any]]]:
    return [
        (channel, payload)
        for channel, payload in captured["events"]
        if channel in _MEMBER_CHANNELS and payload.get("user_id") == str(user_id)
    ]


@pytest.mark.asyncio
async def test_accepting_an_invitation_publishes_member_added(
    async_client: AsyncClient, user_a, user_b, captured, org_with_owner
) -> None:
    _, headers_a = user_a
    user_b_id, _ = user_b
    org_id = org_with_owner["id"]
    captured["events"].clear()

    await _invite_and_accept(async_client, org_id, headers_a, user_b, "admin")

    added = [p for c, p in _member_events(captured, user_b_id)]
    assert len(added) == 1, captured["events"]
    channel = _member_events(captured, user_b_id)[0][0]
    assert channel == events.MEMBER_ADDED_V1
    payload = added[0]
    assert payload["organization_id"] == org_id
    assert payload["user_id"] == str(user_b_id)
    assert payload["role"] == "admin"
    assert isinstance(payload["version"], int) and payload["version"] >= 1
    # The invitation event is still published (additive change).
    assert events.INVITATION_ACCEPTED_V1 in [c for c, _ in captured["events"]]


@pytest.mark.asyncio
async def test_member_versions_increase_across_the_whole_lifecycle(
    async_client: AsyncClient, user_a, user_b, captured, org_with_owner
) -> None:
    owner_id, headers_a = user_a
    user_b_id, _ = user_b
    org_id = org_with_owner["id"]
    # Creating the org announces the owner's membership too.
    owner_added = _member_events(captured, owner_id)
    assert [c for c, _ in owner_added] == [events.MEMBER_ADDED_V1]
    assert owner_added[0][1]["role"] == ROLE_OWNER
    captured["events"].clear()

    await _invite_and_accept(async_client, org_id, headers_a, user_b, "member")
    patched = await async_client.patch(
        f"/api/v1/orgs/{org_id}/members/{user_b_id}",
        json={"role": "admin"},
        headers=headers_a,
    )
    assert patched.status_code == 200, patched.text
    removed = await async_client.delete(
        f"/api/v1/orgs/{org_id}/members/{user_b_id}", headers=headers_a
    )
    assert removed.status_code == 204, removed.text
    # Re-adding the same user must NOT restart the version at 1, or a
    # version-guarded consumer would drop the re-add as older than the removal.
    await _invite_and_accept(async_client, org_id, headers_a, user_b, "member")

    lifecycle = _member_events(captured, user_b_id)
    assert [c for c, _ in lifecycle] == [
        events.MEMBER_ADDED_V1,
        events.MEMBER_ROLE_CHANGED_V1,
        events.MEMBER_REMOVED_V1,
        events.MEMBER_ADDED_V1,
    ]
    versions = [p["version"] for _, p in lifecycle]
    assert all(isinstance(v, int) for v in versions), versions
    assert versions == sorted(set(versions)), versions  # strictly increasing


@pytest.mark.asyncio
async def test_ownership_transfer_events_carry_versions(
    async_client: AsyncClient, user_a, user_b, captured, org_with_owner
) -> None:
    owner_id, headers_a = user_a
    user_b_id, _ = user_b
    org_id = org_with_owner["id"]
    await _invite_and_accept(async_client, org_id, headers_a, user_b, "admin")
    before = {
        uid: _member_events(captured, uid)[-1][1]["version"]
        for uid in (owner_id, user_b_id)
    }
    captured["events"].clear()

    resp = await async_client.post(
        f"/api/v1/orgs/{org_id}/transfer-ownership/{user_b_id}", headers=headers_a
    )
    assert resp.status_code == 200, resp.text

    changes = {
        p["user_id"]: (p["role"], p["version"])
        for c, p in captured["events"]
        if c == events.MEMBER_ROLE_CHANGED_V1
    }
    assert changes[str(user_b_id)][0] == ROLE_OWNER
    assert changes[str(owner_id)][0] == ROLE_ADMIN
    assert changes[str(user_b_id)][1] > before[user_b_id]
    assert changes[str(owner_id)][1] > before[owner_id]


def test_member_event_schemas_declare_the_optional_version() -> None:
    """Additive v1 change: ``version`` is declared, and NOT required, so an
    older producer's version-less event still validates."""
    producers = {cls.channel: cls for cls in events.MemberEvent.__subclasses__()}
    for channel in _MEMBER_CHANNELS:
        schema = producers[channel].model_json_schema()
        assert {"type": "integer", "minimum": 1} in schema["properties"]["version"][
            "anyOf"
        ]
        assert "version" not in schema["required"]
