"""The volatile event bus driver is ``broadcast``; ``redis`` is its deprecated alias.

The ``eventbus`` driver that runs over a ``broadcast`` instance is named after
the capability it runs on. The old spelling ``redis`` keeps working for one
release: the configuration loads it as ``broadcast`` and logs one WARNING that
names the new spelling (``DEPRECATED_DRIVER_ALIASES``). The driver imports
nothing of Redis, so it loads where the client is absent.
"""

from __future__ import annotations

import logging
import sys

import pytest

import sandloom.core.infra.config as config_module
from sandloom.core.config.settings import settings
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.eventing.volatile_provider import BroadcastEventBusProvider
from sandloom.core.infra.active import use_infra
from sandloom.core.infra.config import (
    DEFAULT_INSTANCE,
    InfraConfig,
    InstanceConfig,
    eventbus_driver,
    load_infra_config,
    resolve_driver_aliases,
    use_default_infra,
)
from sandloom.core.infra.driver_aliases import canonical_driver
from sandloom.core.schema.registry import InMemorySchemaRegistry

_LOGGER = "sandloom.core.infra"


def _config(bus_driver: str) -> InfraConfig:
    """The two instances the bus needs, with the memory ``broadcast`` driver."""
    return {
        "broadcast": {
            DEFAULT_INSTANCE: InstanceConfig(
                capability="broadcast", name=DEFAULT_INSTANCE, driver="memory"
            )
        },
        "eventbus": {
            DEFAULT_INSTANCE: InstanceConfig(
                capability="eventbus", name=DEFAULT_INSTANCE, driver=bus_driver
            )
        },
    }


@pytest.fixture(autouse=True)
def fresh_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """No composition selection, no process ``INFRA`` and no WARNING already
    given: each test states its own configuration and sees its own warnings."""
    monkeypatch.setattr(config_module, "_PIN", None)
    monkeypatch.setattr(config_module, "_APP", None)
    monkeypatch.setattr(config_module, "_WARNED_ALIASES", set())
    use_default_infra({})
    monkeypatch.setattr(settings, "infra", {}, raising=False)
    monkeypatch.setattr(settings, "event_bus_backend", None)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == _LOGGER and record.levelno == logging.WARNING
    ]


# ------------------------------------------------------------------ the table
def test_only_the_eventbus_redis_spelling_is_an_alias() -> None:
    assert canonical_driver("eventbus", "redis") == "broadcast"
    assert canonical_driver("eventbus", "broadcast") == "broadcast"
    # The Redis drivers of every other capability keep their name.
    for capability in ("cache", "broadcast", "lease", "atomic_store"):
        assert canonical_driver(capability, "redis") == "redis"
    assert canonical_driver("eventbus", "streams") == "streams"


# -------------------------------------------------------------- the loaders
def test_infra_driver_redis_loads_as_broadcast_and_warns_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    monkeypatch.setattr(
        settings,
        "infra",
        {"eventbus": {"default": {"driver": "redis", "broadcast": "side"}}},
        raising=False,
    )
    cfg = load_infra_config(settings)["eventbus"][DEFAULT_INSTANCE]
    assert (cfg.driver, cfg.options) == ("broadcast", {"broadcast": "side"})
    # The readers that run before the registry see the new name only, and a
    # configuration read again does not repeat the WARNING.
    assert eventbus_driver(settings) == "broadcast"
    load_infra_config(settings)
    (message,) = _warnings(caplog)
    assert 'INFRA={"eventbus": {"default": {"driver": "broadcast"}}}' in message
    assert "'redis'" in message and "later release" in message


def test_the_new_spelling_logs_no_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    monkeypatch.setattr(
        settings,
        "infra",
        {"eventbus": {"default": {"driver": "broadcast"}}},
        raising=False,
    )
    assert load_infra_config(settings)["eventbus"][DEFAULT_INSTANCE].driver == (
        "broadcast"
    )
    assert _warnings(caplog) == []


def test_event_bus_backend_redis_is_the_alias_too(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    monkeypatch.setattr(settings, "event_bus_backend", "redis")
    assert load_infra_config(settings)["eventbus"][DEFAULT_INSTANCE].driver == (
        "broadcast"
    )
    assert eventbus_driver(settings) == "broadcast"
    assert len(_warnings(caplog)) == 1


def test_a_composition_selecting_redis_is_resolved_and_infra_options_layer_on_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    use_default_infra({"eventbus": {"default": {"driver": "redis"}}})
    monkeypatch.setattr(
        settings,
        "infra",
        {"eventbus": {"default": {"broadcast": "side"}}},
        raising=False,
    )
    cfg = load_infra_config(settings)["eventbus"][DEFAULT_INSTANCE]
    # INFRA states no driver: the composition's, resolved, with INFRA's options.
    assert (cfg.driver, cfg.options) == ("broadcast", {"broadcast": "side"})
    assert len(_warnings(caplog)) == 1


def test_only_the_eventbus_capability_is_aliased(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    monkeypatch.setattr(
        settings,
        "infra",
        {"cache": {"default": {"driver": "redis"}}},
        raising=False,
    )
    assert load_infra_config(settings)["cache"][DEFAULT_INSTANCE].driver == "redis"
    assert _warnings(caplog) == []


# ---------------------------------------------------- the registry at startup
def test_an_explicit_configuration_with_the_alias_builds_the_broadcast_driver(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    registry = build_infra_registry(
        InMemorySchemaRegistry(), config=_config("redis"), load_plugins=False
    )
    provider = registry.provider("eventbus")
    assert isinstance(provider, BroadcastEventBusProvider)
    assert provider.driver == "broadcast"
    (message,) = _warnings(caplog)
    assert 'INFRA={"eventbus": {"default": {"driver": "broadcast"}}}' in message


def test_the_new_spelling_builds_with_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    registry = build_infra_registry(
        InMemorySchemaRegistry(), config=_config("broadcast"), load_plugins=False
    )
    assert isinstance(registry.provider("eventbus"), BroadcastEventBusProvider)
    assert _warnings(caplog) == []


def test_every_instance_that_wrote_the_alias_is_warned_about(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    config = _config("redis")
    config["eventbus"]["side"] = InstanceConfig(
        capability="eventbus", name="side", driver="redis"
    )
    resolve_driver_aliases(config)
    assert {cfg.driver for cfg in config["eventbus"].values()} == {"broadcast"}
    assert len(_warnings(caplog)) == 2


# ----------------------------------------------- no Redis package, no import
async def test_broadcast_bus_loads_and_runs_with_the_redis_package_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The driver imports nothing of Redis: its row declares no requirement, so
    the preflight passes where ``redis`` is not importable."""
    monkeypatch.setitem(sys.modules, "redis", None)  # an `import redis` now raises
    registry = build_infra_registry(
        InMemorySchemaRegistry(), config=_config("broadcast"), load_plugins=False
    )
    provider = registry.provider("eventbus")
    assert isinstance(provider, BroadcastEventBusProvider)
    assert provider.required_imports == ()
    # The bus runs over this registry's memory ``broadcast``, not whatever
    # registry an earlier test left active.
    with use_infra(registry):
        await provider.aopen()
        assert (await provider.health()).status == "ok"
        await provider.aclose()
