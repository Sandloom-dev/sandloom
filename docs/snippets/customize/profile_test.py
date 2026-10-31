"""Tests of the profiles module."""

from sandloom.sdk.testing import as_user, deliver


async def test_a_profile_is_saved_and_removed_with_its_user(client, db) -> None:
    user_id, headers = as_user()

    saved = await client.put(
        "/api/v1/profiles/me", json={"display_name": "Ada"}, headers=headers
    )
    assert saved.status_code == 200
    read = await client.get("/api/v1/profiles/me", headers=headers)
    assert read.json() == {"display_name": "Ada"}

    await deliver(
        "identity.user_deleted.v1",
        {
            "event_id": "019a0000-0000-7000-8000-000000000001",
            "event_type": "identity.user_deleted.v1",
            "occurred_at": "2026-10-31T00:00:00Z",
            "schema_version": 1,
            "actor": {"user_id": str(user_id)},
            "trace_id": "test",
            "payload": {"user_id": str(user_id)},
        },
        session=db,
    )
    gone = await client.get("/api/v1/profiles/me", headers=headers)
    assert gone.status_code == 404
