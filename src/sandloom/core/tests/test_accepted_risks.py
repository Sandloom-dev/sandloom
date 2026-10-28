"""``SANDLOOM_ACCEPTED_RISKS`` at the checks that consult it (ADR-0006).

The registry / reporting side is pinned by ``test_config_tiers.py``; this
file proves each consulting check admits the weakening through the risk id
(and still through its deprecated alias until 0.9), and that the relay
refusal holds in prod.
"""

from __future__ import annotations

from typing import Any

import pytest

from sandloom.core.app_factory import outbox_relay_problem
from sandloom.core.auth.tokens import token_signing_problem
from sandloom.core.config.common import Environment
from sandloom.core.config.settings import Settings, settings
from sandloom.core.consumers.callbacks import VolatileBusError, subscribe_to_bus
from sandloom.core.drivers.event_bus_driver import make_event_bus_drivers
from sandloom.core.drivers.log_notifier import log_notifier_features
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.schema.registry import InMemorySchemaRegistry

_STRONG = "9f3b7c1e5a2d4f6b8c0e1a3d5f7b9c2e4a6d8f0b1c3e5a7d9f2b4c6e8a0d1f3b"
_BASE = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/d",
    "CACHE_URL": "redis://cache-redis:6379/0",
    "BROKER_URL": "redis://cache-redis:6379/1",
    "JWT_SECRET": _STRONG,
}


def _settings(**env: Any) -> Settings:
    return Settings(_env_file=None, **{**_BASE, **env})  # type: ignore[call-arg]


@pytest.fixture
def prod(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setattr(settings, "environment", Environment.prod)
    monkeypatch.setattr(settings, "sandloom_accepted_risks", [])
    return monkeypatch


# ------------------------------------------------------------ outbox relay ----


@pytest.mark.parametrize("mode", ["auto", "on"])
def test_relay_running_in_prod_is_fine(mode: str) -> None:
    assert (
        outbox_relay_problem(_settings(ENVIRONMENT="prod", OUTBOX_RELAY_ENABLED=mode))
        is None
    )


def test_relay_off_in_prod_is_refused_unless_accepted() -> None:
    off = _settings(ENVIRONMENT="prod", OUTBOX_RELAY_ENABLED="off")
    problem = outbox_relay_problem(off)
    assert (
        problem is not None and 'SANDLOOM_ACCEPTED_RISKS=["relay_disabled"]' in problem
    )
    accepted = _settings(
        ENVIRONMENT="prod",
        OUTBOX_RELAY_ENABLED="off",
        SANDLOOM_ACCEPTED_RISKS=["relay_disabled"],
    )
    assert outbox_relay_problem(accepted) is None
    # Outside prod the relay is off by default: nothing to refuse.
    assert outbox_relay_problem(_settings(OUTBOX_RELAY_ENABLED="off")) is None


def test_delegated_relay_needs_a_gunicorn_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _settings(ENVIRONMENT="prod", OUTBOX_RELAY_ENABLED="delegated")
    monkeypatch.delenv("SANDLOOM_WORKER_COUNT", raising=False)
    assert outbox_relay_problem(cfg) is not None  # set by hand: no leader
    monkeypatch.setenv("SANDLOOM_WORKER_COUNT", "3")  # gunicorn post_fork sets it
    assert outbox_relay_problem(cfg) is None


# ----------------------------------------------------------- event bus ----


def test_volatile_bus_driver_admitted_in_prod_by_the_risk(
    prod: pytest.MonkeyPatch,
) -> None:
    drivers = make_event_bus_drivers(InMemorySchemaRegistry())
    cfg = InstanceConfig(capability="eventbus", name="side", driver="memory")
    with pytest.raises(RuntimeError, match="volatile_event_bus"):
        drivers["memory"](cfg)
    prod.setattr(settings, "sandloom_accepted_risks", ["volatile_event_bus"])
    assert drivers["memory"](cfg).bus() is not None


class _PubSubLikeBus:
    durable = False
    in_process = False

    async def subscribe(self, channel: str, callback: Any) -> str:
        return "handle"


async def _noop(_env: Any) -> None:
    return None


async def test_volatile_consumers_outside_prod_need_the_risk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "sandloom_accepted_risks", [])
    with pytest.raises(VolatileBusError, match="volatile_event_bus"):
        await subscribe_to_bus(_PubSubLikeBus(), "core.thing.v1", _noop, "g")
    monkeypatch.setattr(settings, "sandloom_accepted_risks", ["volatile_event_bus"])
    assert await subscribe_to_bus(_PubSubLikeBus(), "core.thing.v1", _noop, "g")


# ------------------------------------------------- notifier / token roles ----


def test_log_notifier_channels_in_prod_need_the_risk(prod: pytest.MonkeyPatch) -> None:
    prod.setattr(settings, "sandloom_accepted_risks", [])
    assert log_notifier_features() == frozenset()
    prod.setattr(settings, "sandloom_accepted_risks", ["log_notifier_in_prod"])
    assert "email" in log_notifier_features()


def test_shared_hs_secret_split_in_prod_needs_the_risk() -> None:
    cfg = _settings(ENVIRONMENT="prod")
    problem = token_signing_problem(issuer_loaded=False, cfg=cfg)
    assert problem is not None and "shared_hs_secret_split" in problem
    accepted = _settings(
        ENVIRONMENT="prod", SANDLOOM_ACCEPTED_RISKS=["shared_hs_secret_split"]
    )
    assert token_signing_problem(issuer_loaded=False, cfg=accepted) is None


def test_strict_profile_refuses_the_shared_secret_risk() -> None:
    from sandloom.core.production_profile import config_problems

    errors, _warnings = config_problems(
        _settings(SANDLOOM_ACCEPTED_RISKS=["shared_hs_secret_split"])
    )
    assert any("shared_hs_secret_split" in e for e in errors)
