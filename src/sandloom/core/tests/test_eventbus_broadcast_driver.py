"""The volatile event bus driver is ``broadcast``; ``redis`` is refused by name.

The ``eventbus`` driver that runs over a ``broadcast`` instance is named after
the capability it runs on. The old name ``redis`` has no alias (ADR-0010,
ADR-0016): every way a configuration reaches the kernel refuses it with a
message that names ``broadcast`` and prints no option, so no url or password.
The driver imports nothing of Redis, so it loads where the client is absent.
"""

from __future__ import annotations

import sys

import pytest
from pydantic import ValidationError

import sandloom.core.infra.config as config_module
from sandloom.core.config.eventing import EventingConfig
from sandloom.core.config.removed_drivers import (
    REMOVED_DRIVERS,
    removed_driver_problem,
)
from sandloom.core.config.settings import settings
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.eventing.volatile_provider import BroadcastEventBusProvider
from sandloom.core.infra.active import use_infra
from sandloom.core.infra.config import (
    DEFAULT_INSTANCE,
    InfraConfig,
    InfraConfigError,
    InstanceConfig,
    eventbus_driver,
    load_infra_config,
    use_default_infra,
)
from sandloom.core.infra.registry import InfraRegistry, InfraRegistryError
from sandloom.core.schema.registry import InMemorySchemaRegistry

#: A url that carries a password: no refusal may print it.
_SECRET_URL = "redis://:s3cr3t-pa55@redis.internal:6379/0"
_FIX = 'INFRA={"eventbus": {"default": {"driver": "broadcast"}}}'


def _config(bus_driver: str, **options: object) -> InfraConfig:
    """The two instances the bus needs, with the memory ``broadcast`` driver."""
    return {
        "broadcast": {
            DEFAULT_INSTANCE: InstanceConfig(
                capability="broadcast", name=DEFAULT_INSTANCE, driver="memory"
            )
        },
        "eventbus": {
            DEFAULT_INSTANCE: InstanceConfig(
                capability="eventbus",
                name=DEFAULT_INSTANCE,
                driver=bus_driver,
                options=dict(options),
            )
        },
    }


@pytest.fixture(autouse=True)
def fresh_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """No composition selection and no process ``INFRA``: each test states its
    own configuration."""
    monkeypatch.setattr(config_module, "_PIN", None)
    monkeypatch.setattr(config_module, "_APP", None)
    use_default_infra({})
    monkeypatch.setattr(settings, "infra", {}, raising=False)
    monkeypatch.setattr(settings, "event_bus_backend", None)


def _assert_refusal(error: BaseException) -> None:
    """The refusal names the new spelling, and prints no option or secret."""
    text = str(error)
    assert _FIX in text
    assert "'redis'" in text and "'broadcast'" in text
    assert "s3cr3t" not in text and "redis.internal" not in text


# ------------------------------------------------------------------ the table
def test_only_the_eventbus_redis_driver_is_removed() -> None:
    assert set(REMOVED_DRIVERS) == {("eventbus", "redis")}
    assert removed_driver_problem("eventbus", "default", "redis") is not None
    # The Redis drivers of every other capability keep their name, and the
    # new name is no removed one.
    for capability in ("cache", "broadcast", "lease", "atomic_store"):
        assert removed_driver_problem(capability, "default", "redis") is None
    assert removed_driver_problem("eventbus", "default", "broadcast") is None
    assert removed_driver_problem("eventbus", "default", "streams") is None


def test_the_refusal_names_the_instance_it_refuses() -> None:
    problem = removed_driver_problem("eventbus", "side", "redis")
    assert problem is not None
    assert "instance 'side'" in problem
    assert '{"eventbus": {"side": {"driver": "broadcast"}}}' in problem


# -------------------------------------------------------------- the loaders
def test_infra_driver_redis_is_refused_without_printing_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "infra",
        {"eventbus": {"default": {"driver": "redis", "url": _SECRET_URL}}},
        raising=False,
    )
    with pytest.raises(InfraConfigError) as refused:
        load_infra_config(settings)
    _assert_refusal(refused.value)


def test_infra_redis_on_a_named_instance_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "infra",
        {"eventbus": {"side": {"driver": "redis", "url": _SECRET_URL}}},
        raising=False,
    )
    with pytest.raises(InfraConfigError, match="instance 'side'"):
        load_infra_config(settings)


def test_the_new_spelling_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "infra",
        {"eventbus": {"default": {"driver": "broadcast", "broadcast": "side"}}},
        raising=False,
    )
    cfg = load_infra_config(settings)["eventbus"][DEFAULT_INSTANCE]
    assert (cfg.driver, cfg.options) == ("broadcast", {"broadcast": "side"})
    assert eventbus_driver(settings) == "broadcast"


@pytest.mark.parametrize("from_app", [False, True])
def test_a_composition_selecting_redis_is_refused(from_app: bool) -> None:
    with pytest.raises(InfraConfigError) as refused:
        use_default_infra(
            {"eventbus": {"default": {"driver": "redis", "url": _SECRET_URL}}},
            from_app=from_app,
        )
    _assert_refusal(refused.value)


def test_event_bus_backend_redis_is_refused_by_name_not_as_a_bare_value() -> None:
    with pytest.raises(ValidationError) as refused:
        EventingConfig.model_validate({"EVENT_BUS_BACKEND": "redis"})
    text = str(refused.value)
    assert (
        "EVENT_BUS_BACKEND=redis: the 'redis' event bus driver was removed in "
        "0.7.0a15 and is now named 'broadcast'; set EVENT_BUS_BACKEND=broadcast, "
        'or in INFRA write {"eventbus": {"default": {"driver": "broadcast"}}}'
    ) in text
    # The surrounding spaces of the value are not part of the name.
    with pytest.raises(ValidationError, match="EVENT_BUS_BACKEND=redis: the 'redis'"):
        EventingConfig.model_validate({"EVENT_BUS_BACKEND": " redis "})
    # The other names still validate; an unknown one is still a bare refusal.
    for name in ("memory", "broadcast", "streams"):
        assert EventingConfig.model_validate({"EVENT_BUS_BACKEND": name})
    with pytest.raises(ValidationError):
        EventingConfig.model_validate({"EVENT_BUS_BACKEND": "kafka"})


def test_event_bus_backend_redis_that_bypassed_validation_is_refused_at_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "event_bus_backend", "redis")
    with pytest.raises(InfraConfigError) as refused:
        load_infra_config(settings)
    _assert_refusal(refused.value)


def test_only_the_eventbus_capability_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "infra",
        {
            "cache": {"default": {"driver": "redis"}},
            "broadcast": {"default": {"driver": "redis"}},
            "lease": {"beat": {"driver": "redis"}},
            "atomic_store": {"security": {"driver": "redis"}},
        },
        raising=False,
    )
    config = load_infra_config(settings)
    assert config["cache"][DEFAULT_INSTANCE].driver == "redis"
    assert config["broadcast"][DEFAULT_INSTANCE].driver == "redis"
    assert config["lease"]["beat"].driver == "redis"
    assert config["atomic_store"]["security"].driver == "redis"


# ---------------------------------------------------- the registry entry points
def test_a_registry_built_with_a_hand_made_redis_bus_is_refused() -> None:
    with pytest.raises(InfraRegistryError) as refused:
        InfraRegistry(_config("redis", url=_SECRET_URL))
    _assert_refusal(refused.value)


def test_set_config_refuses_redis_and_keeps_the_configuration_it_had() -> None:
    registry = InfraRegistry(_config("memory"))
    with pytest.raises(InfraRegistryError) as refused:
        registry.set_config(_config("redis", url=_SECRET_URL))
    _assert_refusal(refused.value)
    kept = registry.instance_config("eventbus")
    assert kept is not None and kept.driver == "memory"


def test_configure_instance_refuses_redis_and_keeps_the_instance_it_had() -> None:
    registry = InfraRegistry(_config("memory"))
    with pytest.raises(InfraRegistryError) as refused:
        registry.configure_instance(
            InstanceConfig(
                capability="eventbus",
                name=DEFAULT_INSTANCE,
                driver="redis",
                options={"url": _SECRET_URL},
            )
        )
    _assert_refusal(refused.value)
    kept = registry.instance_config("eventbus")
    assert kept is not None and kept.driver == "memory"


def test_the_registry_still_takes_the_redis_driver_of_other_capabilities() -> None:
    registry = InfraRegistry()
    registry.configure_instance(
        InstanceConfig(capability="cache", name=DEFAULT_INSTANCE, driver="redis")
    )
    cache = registry.instance_config("cache")
    assert cache is not None and cache.driver == "redis"


def test_build_infra_registry_refuses_an_explicit_redis_bus() -> None:
    with pytest.raises(InfraRegistryError) as refused:
        build_infra_registry(
            InMemorySchemaRegistry(),
            config=_config("redis", url=_SECRET_URL),
            load_plugins=False,
        )
    _assert_refusal(refused.value)


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
