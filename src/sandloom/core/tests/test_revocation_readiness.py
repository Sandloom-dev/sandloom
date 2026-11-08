"""``/ready`` judges the revocation and block consumers (ADR-0012 decision 5, A9).

Not ready when the applier is needed and missing, when the oldest pending
``core.auth.tokens_revoked.v1`` / ``core.auth.user_blocked.v1`` event is older
than the access-token lifetime, when one is dead-lettered, or when the
consumer is not running. A younger backlog is transient: ready, exported and
logged at WARNING above half the bound. Recovery returns ``/ready`` to 200.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from sandloom.core.app_factory import _install_ops_endpoints
from sandloom.core.app_factory import revocation_applier as applier
from sandloom.core.config.settings import settings
from sandloom.core.consumers.runtime import ConsumerRuntimeSnapshot, ConsumerStatus
from sandloom.core.eventing.memory_transport import MemoryTransport
from sandloom.core.eventing.streams_bus import StreamsEventBus
from sandloom.core.facade import CoreFacade
from sandloom.core.schema.registry import InMemorySchemaRegistry

REVOKED = "core.auth.tokens_revoked.v1"
BLOCKED = "core.auth.user_blocked.v1"
GROUP = "sandloom:auth@tokens_revoked@api"
BLOCK_GROUP = "identity:sessions@user_blocked"
BOUND = 15 * 60.0  # ACCESS_TOKEN_EXPIRE_MINUTES=15
_TOKEN = "kb09-readiness-internal-token-0123456789abcdef"


def _status(
    channel: str = REVOKED,
    group: str = GROUP,
    *,
    state: str = "running",
    age: float | None = 0.0,
) -> ConsumerStatus:
    return ConsumerStatus(
        name=group.rsplit(":", 1)[-1],
        channel=channel,
        kind="side_effect",
        required=True,
        state=state,
        group=group,
        oldest_unacked_age_seconds=age,
    )


def _judge(
    *statuses: ConsumerStatus,
    dead: dict[tuple[str, str], int] | None = None,
    needed: bool = True,
) -> applier.RevocationReadiness:
    return applier.evaluate_revocation_readiness(
        statuses,
        dead or {},
        bound_seconds=BOUND,
        needed=needed,
        expected_group=GROUP if needed else None,
    )


def test_a_running_applier_without_backlog_is_ready() -> None:
    result = _judge(_status(), _status(BLOCKED, BLOCK_GROUP))
    assert result.ready and result.reasons == ()


def test_a_transient_backlog_stays_ready() -> None:
    """Younger than the access-token lifetime: a revoked token it covers
    still expires before the event is late; ready, the lag reported."""
    result = _judge(_status(age=BOUND * 0.9))
    assert result.ready
    assert result.lag_seconds == {GROUP: BOUND * 0.9}


@pytest.mark.parametrize("channel,group", [(REVOKED, GROUP), (BLOCKED, BLOCK_GROUP)])
def test_a_backlog_older_than_the_token_lifetime_is_not_ready(
    channel: str, group: str
) -> None:
    result = _judge(_status(), _status(channel, group, age=BOUND + 1))
    assert not result.ready
    assert any("past the access-token lifetime" in r for r in result.reasons)


@pytest.mark.parametrize("channel,group", [(REVOKED, GROUP), (BLOCKED, BLOCK_GROUP)])
def test_one_dead_letter_is_not_ready(channel: str, group: str) -> None:
    result = _judge(_status(), _status(channel, group), dead={(channel, group): 1})
    assert not result.ready
    assert result.dead_letters == {group: 1}


def test_another_groups_dead_letter_does_not_count() -> None:
    """The DLQ is per stream; only this deployment's groups judge it."""
    other = (REVOKED, "sandloom:auth@tokens_revoked@billing")
    assert _judge(_status(), dead={other: 3}).ready


@pytest.mark.parametrize("state", ["failed", "retrying", "stopped", "registered"])
def test_a_consumer_that_is_not_running_is_not_ready(state: str) -> None:
    result = _judge(_status(state=state))
    assert not result.ready
    assert any(state in r for r in result.reasons)


def test_a_needed_applier_that_is_missing_is_not_ready() -> None:
    result = _judge(_status(BLOCKED, BLOCK_GROUP))
    assert not result.ready
    assert any("not registered" in r for r in result.reasons)
    # an applier under another deployment's group does not stand in for it
    other = _status(group="sandloom:auth@tokens_revoked@billing")
    assert not _judge(other).ready


def test_without_a_shared_bus_nothing_is_required() -> None:
    """The lean composition: no applier needed, nothing to judge, ready."""
    assert _judge(needed=False).ready


def test_recovery_returns_ready() -> None:
    assert not _judge(_status(age=BOUND * 2)).ready
    assert not _judge(_status(state="failed")).ready
    assert _judge(_status(age=0.0)).ready


# --------------------------------------------------------------------------- #
# the probe: dead letters read from the bus, the WARNING, the gauge
# --------------------------------------------------------------------------- #
class _Runtime:
    def __init__(self, *statuses: ConsumerStatus) -> None:
        self.statuses = statuses

    def snapshot(self) -> ConsumerRuntimeSnapshot:
        unavailable = tuple(s.name for s in self.statuses if s.state != "running")
        return ConsumerRuntimeSnapshot(
            ready=not unavailable,
            required_unavailable=unavailable,
            consumers=self.statuses,
        )


def _shared_bus_cfg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "event_bus_backend", "streams")
    monkeypatch.setattr(settings, "sandloom_service_name", "api")
    monkeypatch.setattr(settings, "access_token_expire_minutes", 15)


def _envelope() -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "channel": REVOKED,
        "schema_version": "v1",
        "trace_id": None,
        "emitted_at": "2026-01-01T00:00:00+00:00",
        "payload": {"user_id": "u", "valid_after": "x"},
    }


async def test_the_probe_counts_this_deployments_dead_letters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _shared_bus_cfg(monkeypatch)
    transport = MemoryTransport()
    core = CoreFacade(app=FastAPI())
    core.set_event_bus(StreamsEventBus(InMemorySchemaRegistry(), transport))
    probe = applier.RevocationReadinessProbe(core, settings, ttl=0.0)
    runtime = _Runtime(_status())
    assert (await probe(runtime)).ready
    transport._dlq[REVOKED] = [
        ("1-0", _envelope(), 1, "sandloom:auth@tokens_revoked@billing"),
        ("2-0", _envelope(), 1, GROUP),
    ]
    result = await probe(runtime)
    assert not result.ready
    assert result.dead_letters == {GROUP: 1}
    transport._dlq[REVOKED] = []
    assert (await probe(runtime)).ready


async def test_the_probe_warns_above_half_the_bound_and_exports_the_lag(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _shared_bus_cfg(monkeypatch)
    core = CoreFacade(app=FastAPI())
    core.set_event_bus(StreamsEventBus(InMemorySchemaRegistry(), MemoryTransport()))
    probe = applier.RevocationReadinessProbe(core, settings, ttl=0.0)
    result = await probe(_Runtime(_status(age=BOUND * 0.6)))
    assert result.ready
    assert any(
        r.levelname == "WARNING" and "over half" in r.getMessage()
        for r in caplog.records
    )
    gauge = applier._GAUGE_CONSUMER_LAG.labels(GROUP, REVOKED)
    assert gauge._value.get() == pytest.approx(BOUND * 0.6)


async def _ready(runtime: Any) -> Any:
    application = FastAPI()
    core = CoreFacade(app=application)
    core.set_event_bus(StreamsEventBus(InMemorySchemaRegistry(), MemoryTransport()))
    application.state.consumer_runtime = runtime
    _install_ops_endpoints(application, core)
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        return await client.get("/ready", headers={"X-Internal-Token": _TOKEN})


@pytest.mark.parametrize(
    "statuses,ready",
    [
        ((_status(age=BOUND * 0.5),), True),  # transient backlog
        ((_status(age=BOUND + 60),), False),  # aged backlog
        ((_status(state="failed"),), False),  # stopped consumer
        ((), False),  # needed and missing
        ((_status(),), True),  # recovered
    ],
)
async def test_ready_answers_by_the_criteria(
    monkeypatch: pytest.MonkeyPatch,
    statuses: tuple[ConsumerStatus, ...],
    ready: bool,
) -> None:
    _shared_bus_cfg(monkeypatch)
    monkeypatch.setattr(settings, "internal_api_token", _TOKEN)
    monkeypatch.setattr(settings, "health_cache_ttl_seconds", 0.0)
    monkeypatch.setattr(settings, "sandloom_modules", [])
    monkeypatch.setattr(settings, "module_required", [])

    import sandloom.core.app_factory.ops as ops
    from sandloom.core.descriptors import HealthResult

    async def _healthy(_core: Any) -> dict[str, HealthResult]:
        return {"core": HealthResult(status="ok")}

    monkeypatch.setattr(ops, "aggregate_health", _healthy)
    response = await _ready(_Runtime(*statuses))
    body = response.json()
    assert body["revocation_consumers_ready"] is ready, body
    if not ready:
        assert response.status_code == 503
        assert body["revocation_consumers"]["reasons"]
    else:
        assert response.status_code == 200, body


def test_the_runtime_status_carries_the_oldest_unacked_age() -> None:
    """``ConsumerStatus`` exposes the bus's observed age (``None`` unknown)."""
    assert (
        ConsumerStatus(
            name="n", channel="c", kind="side_effect", required=True, state="running"
        ).oldest_unacked_age_seconds
        is None
    )
