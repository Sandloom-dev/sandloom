"""Owner-console operations API: audit keyset, outbox/Streams DLQ, runtime views.

Real request UoW (``real_uow_client``) against the test Postgres/Redis: the
views read committed rows, and the commands commit their effect together with
the required audit row.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.core.admin.api.platform import safe_error
from sandloom.core.sql_alchemy.models.audit import AuditLogEntry
from sandloom.core.sql_alchemy.models.outbox import OutboxMessage
from sandloom.core.testing import make_user_headers
from sandloom.core.testing.fixtures import *  # noqa: F403 - DB/app fixtures for this module

SOURCES = ("identity", "tenancy", "billing")
VIEWS = (
    "outbox/dead",
    "consumers",
    "projections",
    "contracts",
    "modules",
    "settings",
)
SECRETS = (
    "postgresql",
    "redis://",
    "internal_token",
    "base_url",
    "jwt_secret",
    "hunter2",
)


def operator() -> tuple[uuid.UUID, dict[str, str]]:
    return make_user_headers(roles=["admin"], claims_sources=["identity"], tenant=False)


def key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


@pytest.mark.asyncio
@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("view", VIEWS)
async def test_views_are_operator_only_and_leak_no_secrets(
    real_uow_client: AsyncClient, source: str, view: str
) -> None:
    path = f"/api/v1/platform/{source}/{view}"
    assert (await real_uow_client.get(path)).status_code == 401
    _, tenant_user = make_user_headers()
    assert (await real_uow_client.get(path, headers=tenant_user)).status_code == 403
    _, forged = make_user_headers(roles=["admin"], claims_sources=[], tenant=False)
    assert (await real_uow_client.get(path, headers=forged)).status_code == 403
    response = await real_uow_client.get(path, headers=operator()[1])
    assert response.status_code == 200, response.text
    assert not any(secret in response.text.lower() for secret in SECRETS), response.text


@pytest.mark.asyncio
async def test_runtime_views_describe_this_process(
    real_uow_client: AsyncClient,
) -> None:
    headers = operator()[1]
    base = "/api/v1/platform/identity"
    modules = (await real_uow_client.get(f"{base}/modules", headers=headers)).json()
    assert {"name": "sandloom_identity", "status": "loaded"}.items() <= next(
        m for m in modules if m["name"] == "sandloom_identity"
    ).items()
    contracts = (await real_uow_client.get(f"{base}/contracts", headers=headers)).json()
    assert "tenancy.membership.v1" in contracts["provided"]
    assert {c["resolution"] for c in contracts["consumed"]} <= {
        "local",
        "remote",
        "missing",
    }
    settings_view = (
        await real_uow_client.get(f"{base}/settings", headers=headers)
    ).json()
    keys = {item["key"] for item in settings_view["settings"]}
    assert "environment" in keys and "jwt_secret" not in keys
    assert settings_view["feature_flags"] == "not_configured"
    assert all(i["status"] for i in settings_view["infrastructure"])
    consumers = (await real_uow_client.get(f"{base}/consumers", headers=headers)).json()
    assert all({"name", "channel", "state", "required"} <= c.keys() for c in consumers)


@pytest.mark.asyncio
async def test_audit_is_keyset_paged_with_prefix_and_time_filters(
    real_uow_client: AsyncClient, test_db: AsyncSession
) -> None:
    resource = str(uuid.uuid4())
    start = datetime(2026, 10, 1, 12, 0)
    actions = [
        "ops.test_a.one",
        "ops.test_a.two",
        "ops.testXa.three",
        "ops.other",
        "ops.test_a.four",
    ]
    for index, action in enumerate(actions):
        test_db.add(
            AuditLogEntry(
                occurred_at=start + timedelta(minutes=index),
                actor_type="system",
                action=action,
                resource_type="probe",
                resource_id=resource,
            )
        )
    await test_db.commit()
    headers = operator()[1]
    url = "/api/v1/platform/identity/audit"
    seen: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"resource_id": resource, "limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = await real_uow_client.get(url, params=params, headers=headers)
        assert page.status_code == 200, page.text
        seen += [row["action"] for row in page.json()]
        cursor = page.headers.get("X-Next-Cursor")
        if not cursor:
            break
    assert seen == list(reversed(actions))
    # "_" in a prefix is literal, not a LIKE wildcard ("ops.testXa" excluded).
    prefixed = await real_uow_client.get(
        url,
        params={"resource_id": resource, "action_prefix": "ops.test_a."},
        headers=headers,
    )
    assert [r["action"] for r in prefixed.json()] == [
        "ops.test_a.four",
        "ops.test_a.two",
        "ops.test_a.one",
    ]
    window = await real_uow_client.get(
        url,
        params={
            "resource_id": resource,
            "since": (start + timedelta(minutes=1)).replace(tzinfo=UTC).isoformat(),
            "until": (start + timedelta(minutes=3)).replace(tzinfo=UTC).isoformat(),
        },
        headers=headers,
    )
    assert [r["action"] for r in window.json()] == [
        "ops.testXa.three",
        "ops.test_a.two",
    ]
    assert "data" not in window.json()[0]
    bad = await real_uow_client.get(
        url, params={"cursor": "not-a-cursor"}, headers=headers
    )
    assert bad.status_code == 400


async def _dead_row(test_db: AsyncSession) -> uuid.UUID:
    row = OutboxMessage(
        channel="ops.test.dead.v1",
        payload={"probe": True},
        delivery_attempts=12,
        last_error="BusPublishError: redis://user:hunter2@cache:6379/0 password=hunter2",
        dead_at=datetime(2026, 10, 21, 8, 0),
    )
    test_db.add(row)
    await test_db.commit()
    return row.id


@pytest.mark.asyncio
async def test_outbox_requeue_is_audited_atomically_and_idempotent(
    real_uow_client: AsyncClient, test_db: AsyncSession, sandloom_cache: Any
) -> None:
    row_id = await _dead_row(test_db)
    actor, headers = operator()
    base = "/api/v1/platform/identity/outbox"
    listed = await real_uow_client.get(f"{base}/dead", headers=headers)
    entry = next(r for r in listed.json() if r["id"] == str(row_id))
    assert entry["attempts"] == 12
    assert "hunter2" not in entry["last_error"] and "<redacted" in entry["last_error"]

    body = {"ids": [str(row_id)], "reason": "Broker recovered, INC-7"}
    missing = await real_uow_client.post(f"{base}/requeue", json=body, headers=headers)
    assert missing.status_code == 400
    command = {**headers, **key()}
    first = await real_uow_client.post(f"{base}/requeue", json=body, headers=command)
    again = await real_uow_client.post(f"{base}/requeue", json=body, headers=command)
    assert first.status_code == again.status_code == 200, first.text
    assert first.json() == {"requested": 1, "applied": 1}
    assert again.headers.get("Idempotent-Replay") == "true"
    test_db.expire_all()
    stored = await test_db.get(OutboxMessage, row_id)
    assert (
        stored is not None and stored.dead_at is None and stored.delivery_attempts == 0
    )
    audits = await test_db.scalar(
        select(func.count())
        .select_from(AuditLogEntry)
        .where(
            AuditLogEntry.action == "core.outbox.requeued",
            AuditLogEntry.actor_id == actor,
        )
    )
    assert audits == 1

    _, tenant_user = make_user_headers()
    denied = await real_uow_client.post(
        f"{base}/requeue", json=body, headers={**tenant_user, **key()}
    )
    assert denied.status_code == 403


class _Snapshot:
    def __init__(self, channel: str) -> None:
        self.consumers = (
            SimpleNamespace(
                name="probe@channel",
                channel=channel,
                group="identity:probe@channel",
                kind="side_effect",
                module="sandloom_identity",
                required=True,
                state="running",
                attempts=0,
                lag=0,
                pending=0,
                last_connected_at=None,
                last_error=None,
            ),
        )
        self.required_unavailable = ()


class _Runtime:
    def __init__(self, channel: str) -> None:
        self.channel = channel

    def snapshot(self) -> _Snapshot:
        return _Snapshot(self.channel)


class _Transport:
    """A durable transport double recording the CLI-equivalent calls."""

    def __init__(self) -> None:
        self.replays: list[dict[str, Any]] = []
        self.fail = False

    async def list_dlq(self, stream: str, *, count: int = 100) -> list[dict[str, Any]]:
        return [
            {
                "id": "1-0",
                "group": "identity:probe@channel",
                "event_id": "e-1",
                "attempts": 5,
                "reason": "max_deliveries",
                "replayable": True,
            }
        ]

    async def replay_dlq(
        self,
        stream: str,
        *,
        ids: list[str] | None = None,
        count: int = 100,
        group: str | None = None,
    ) -> int:
        if self.fail:
            raise ConnectionError("redis://cache:6379 down")
        self.replays.append(
            {"stream": stream, "ids": ids, "count": count, "group": group}
        )
        return len(ids or [])


@pytest.mark.asyncio
@pytest.mark.sandloom_allow_server_errors  # asserts the masked 500 itself
async def test_stream_dlq_replay_targets_the_group_and_audits_first(
    real_uow_client: AsyncClient,
    sandloom_app: Any,
    test_db: AsyncSession,
    sandloom_cache: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = "ops.probe.event.v1"
    transport = _Transport()
    monkeypatch.setattr(
        sandloom_app.state, "consumer_runtime", _Runtime(channel), raising=False
    )
    monkeypatch.setattr(
        sandloom_app.state.core.event_bus, "transport", transport, raising=False
    )
    actor, headers = operator()
    base = "/api/v1/platform/identity/streams"
    unknown = await real_uow_client.get(
        f"{base}/dlq", params={"channel": "nobody.consumes.this.v1"}, headers=headers
    )
    assert unknown.status_code == 404
    listed = await real_uow_client.get(
        f"{base}/dlq", params={"channel": channel}, headers=headers
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["group"] == "identity:probe@channel"

    body = {
        "channel": channel,
        "group": "identity:probe@channel",
        "ids": ["1-0"],
        "reason": "Handler fixed",
    }
    done = await real_uow_client.post(
        f"{base}/replay", json=body, headers={**headers, **key()}
    )
    assert done.status_code == 200, done.text
    assert done.json() == {"requested": 1, "applied": 1}
    assert transport.replays == [
        {
            "stream": channel,
            "ids": ["1-0"],
            "count": 1,
            "group": "identity:probe@channel",
        }
    ]

    transport.fail = True
    failed = await real_uow_client.post(
        f"{base}/replay", json=body, headers={**headers, **key()}
    )
    assert failed.status_code >= 500
    audits = await test_db.scalar(
        select(func.count())
        .select_from(AuditLogEntry)
        .where(
            AuditLogEntry.action == "core.streams.dlq_replayed",
            AuditLogEntry.actor_id == actor,
        )
    )
    # The failed replay rolled its audit row back: exactly one remains.
    assert audits == 1


def test_safe_error_redacts_urls_and_secrets() -> None:
    text = "ConnectionError: postgresql+asyncpg://u:p@db/x token=abc " + "x" * 400
    cleaned = safe_error(text)
    assert cleaned is not None
    assert "postgresql" not in cleaned and "abc" not in cleaned
    assert len(cleaned) <= 240
    assert safe_error(None) is None


@pytest.mark.asyncio
async def test_outbox_inspection_pages_past_200_without_requeue(
    real_uow_client: AsyncClient, test_db: AsyncSession
) -> None:
    channel = f"paging.{uuid.uuid4().hex}.v1"
    rows = [
        OutboxMessage(
            channel=channel,
            payload={},
            dead_at=datetime(2026, 10, 21, 8),
            delivery_attempts=12,
        )
        for _ in range(203)
    ]
    test_db.add_all(rows)
    await test_db.commit()
    _, headers = operator()
    url = "/api/v1/platform/identity/outbox/dead"
    first = await real_uow_client.get(
        url, params={"channel": channel, "limit": 200}, headers=headers
    )
    assert first.status_code == 200, first.text
    cursor = first.headers.get("X-Next-Cursor")
    assert cursor, "the operator must be able to inspect the rest without requeueing"
    second = await real_uow_client.get(
        url,
        params={"channel": channel, "limit": 200, "cursor": cursor},
        headers=headers,
    )
    assert second.status_code == 200, second.text
    assert len(first.json()) == 200 and len(second.json()) == 3
    assert len({r["id"] for r in first.json() + second.json()}) == 203
    assert not second.headers.get("X-Next-Cursor")
