"""Built-in drivers declare once, on the class, and are checked like external
ones (ADR-0016, spec 2.2-2.4 and 2.6)."""

from __future__ import annotations

import importlib
from typing import Any, ClassVar

import pytest

from sandloom.core.descriptors import HealthResult
from sandloom.core.drivers import bootstrap
from sandloom.core.drivers.bootstrap import (
    BUILTIN_DRIVER_ROWS,
    register_builtin_drivers,
)
from sandloom.core.infra.access import atomic_store
from sandloom.core.infra.active import use_infra
from sandloom.core.infra.capabilities import (
    BUILTIN_CAPABILITIES,
    NOTIFICATION_CAPABILITY,
)
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.contracts import Notification, NotifierProvider
from sandloom.core.infra.extra_capabilities import EXTRA_CAPABILITIES
from sandloom.core.infra.ports._base import InfraProvider
from sandloom.core.infra.registry import (
    DevelopmentOnlyDriverRefused,
    DriverLoadError,
    InfraRegistry,
    InstanceNotConfiguredError,
)

_SPECS = {spec.name: spec for spec in (*BUILTIN_CAPABILITIES, *EXTRA_CAPABILITIES)}


def _delivered_rows() -> list[tuple[str, str, type[InfraProvider]]]:
    """The built-in rows whose class this build has, with the class."""
    found = []
    for capability, driver, target, _req, _install, delivered_by in BUILTIN_DRIVER_ROWS:
        module_name, _, attribute = target.partition(":")
        if delivered_by:
            try:
                module = importlib.import_module(module_name)
            except ModuleNotFoundError:
                continue
        else:
            module = importlib.import_module(module_name)
        found.append((capability, driver, getattr(module, attribute)))
    return found


@pytest.mark.parametrize(
    ("capability", "driver", "cls"),
    _delivered_rows(),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_a_built_in_declares_once_on_its_class(
    capability: str, driver: str, cls: type[InfraProvider]
) -> None:
    spec = _SPECS[capability]
    assert issubclass(cls, spec.provider_contract)
    version = cls.contract_version
    assert isinstance(version, int) and not isinstance(version, bool)
    assert version == spec.major, f"{capability}/{driver} is for {version}"
    assert cls.supported_features <= spec.features
    # features() is never overridden: the instance reports features_for(options).
    owners = [k for k in cls.__mro__ if "features" in vars(k)]
    assert owners == [InfraProvider], f"{cls.__name__} overrides features()"
    assert isinstance(cls.development_only, bool)
    for operation in cls.performance_hints:
        assert callable(getattr(cls, operation, None)), operation


def test_every_built_in_row_is_registered_lazily() -> None:
    registry = InfraRegistry(strict_capabilities=True)
    register_builtin_drivers(registry, None)  # type: ignore[arg-type]
    for capability, driver, *_rest in BUILTIN_DRIVER_ROWS:
        assert registry.has_driver(capability, driver)


def test_the_sql_dialects_of_the_database_row_are_the_drivers() -> None:
    from sandloom.core.drivers.sqlalchemy_database import (
        SQLALCHEMY_SQL_DIALECTS,
    )

    assert bootstrap.SQLALCHEMY_SQL_DIALECTS == SQLALCHEMY_SQL_DIALECTS


def _registry(**config: InstanceConfig) -> InfraRegistry:
    registry = InfraRegistry(strict_capabilities=True)
    register_builtin_drivers(registry, None)  # type: ignore[arg-type]
    registry.set_config(
        {
            cfg.capability: {
                n: c for n, c in config.items() if c.capability == cfg.capability
            }
            for cfg in config.values()
        }
    )
    return registry


def test_a_row_not_delivered_yet_is_refused_naming_its_slice() -> None:
    registry = _registry(default=InstanceConfig("cache", "default", "memory", {}))
    with pytest.raises(DriverLoadError, match="idk-cache-contract") as refused:
        registry.validate()
    assert refused.value.sandloom_code == "SANDLOOM-B022"


class _Notifier(NotifierProvider):
    contract_version = 2
    supported_features = frozenset({"email"})

    async def send(self, notification: Notification) -> None:
        return None


def test_a_built_in_with_a_string_version_is_refused() -> None:
    class Old(_Notifier):
        contract_version = "v1"  # type: ignore[assignment]

    registry = InfraRegistry(strict_capabilities=True)
    registry.register_capability(NOTIFICATION_CAPABILITY)
    with pytest.raises(DriverLoadError, match="integer major"):
        registry.register_driver("notification", "old", Old)


def test_a_built_in_for_another_major_is_refused() -> None:
    class Old(_Notifier):
        contract_version = 1

    registry = InfraRegistry(strict_capabilities=True)
    registry.register_capability(NOTIFICATION_CAPABILITY)
    with pytest.raises(DriverLoadError, match="written for contract_version 1"):
        registry.register_driver("notification", "old", Old)


def test_the_registry_reads_features_from_the_class() -> None:
    class PerOption(_Notifier):
        supported_features = frozenset({"email", "sms"})

        @classmethod
        def features_for(cls, options: Any) -> frozenset[str]:
            return frozenset({"sms"}) if options.get("sms") else frozenset({"email"})

    registry = InfraRegistry(strict_capabilities=True)
    registry.register_capability(NOTIFICATION_CAPABILITY)
    registry.register_driver("notification", "x", PerOption)
    registry.set_config(
        {
            "notification": {
                "a": InstanceConfig("notification", "a", "x", {}),
                "b": InstanceConfig("notification", "b", "x", {"sms": True}),
            }
        }
    )
    assert registry.declared_features("notification", "a") == {"email"}
    assert registry.declared_features("notification", "b") == {"sms"}
    assert registry.provider("notification", "b").features() == {"sms"}


class _DevOnly(_Notifier):
    development_only: ClassVar[bool] = True


def _prod(monkeypatch: pytest.MonkeyPatch, *, prod: bool) -> None:
    from sandloom.core.config.settings import settings

    monkeypatch.setattr(type(settings), "is_prod", property(lambda self: prod))


def test_a_development_only_driver_is_refused_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = InfraRegistry(strict_capabilities=True)
    registry.register_capability(NOTIFICATION_CAPABILITY)
    registry.register_driver("notification", "memory", _DevOnly)
    registry.set_config(
        {
            "notification": {
                "default": InstanceConfig("notification", "default", "memory")
            }
        }
    )
    _prod(monkeypatch, prod=False)
    registry.validate()
    assert isinstance(registry.provider("notification"), _DevOnly)
    registry.set_config(
        {
            "notification": {
                "default": InstanceConfig("notification", "default", "memory")
            }
        }
    )
    _prod(monkeypatch, prod=True)
    with pytest.raises(DevelopmentOnlyDriverRefused) as refused:
        registry.validate()
    assert refused.value.sandloom_code == "SANDLOOM-B022"
    with pytest.raises(DevelopmentOnlyDriverRefused):
        registry.provider("notification")


def test_the_log_notifier_is_not_development_only() -> None:
    # Orchestrator decision: it keeps the accepted risk log_notifier_in_prod.
    from sandloom.core.drivers.log_notifier import LogNotifierProvider

    assert LogNotifierProvider.development_only is False


class _Leaky(_Notifier):
    async def health(self) -> HealthResult:
        raise ConnectionError("redis://user:s3cret-marker@host:6379/0 refused")


@pytest.mark.asyncio
async def test_the_health_failure_record_carries_the_type_not_the_text() -> None:
    registry = InfraRegistry(strict_capabilities=True)
    registry.register_capability(NOTIFICATION_CAPABILITY)
    registry.register_driver("notification", "leaky", _Leaky)
    registry.set_config(
        {
            "notification": {
                "default": InstanceConfig("notification", "default", "leaky")
            }
        }
    )
    result = (await registry.health_all())["notification:default"]
    assert result.status == "fail"
    assert result.details == {"error": "ConnectionError"}
    assert "s3cret-marker" not in repr(result)


@pytest.mark.asyncio
async def test_aopen_all_opens_the_event_bus() -> None:
    opened: list[str] = []

    class Bus(InfraProvider):
        capability = "eventbus"
        contract_version = 1

        async def aopen(self) -> None:
            opened.append(self.name)

    registry = InfraRegistry()
    registry.register_driver("eventbus", "probe", Bus)
    registry.set_config(
        {"eventbus": {"default": InstanceConfig("eventbus", "default", "probe")}}
    )
    await registry.aopen_all(strict=True)
    assert opened == ["default"]


def test_the_idempotency_store_is_the_security_store_unless_configured() -> None:
    built: list[str] = []

    class Store(InfraProvider):
        capability = "atomic_store"
        contract_version = 1

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            built.append(self.name)

    registry = InfraRegistry()
    registry.register_driver("atomic_store", "probe", Store)
    registry.set_config(
        {
            "atomic_store": {
                "security": InstanceConfig("atomic_store", "security", "probe")
            }
        }
    )
    with use_infra(registry):
        assert atomic_store("idempotency") is atomic_store("security")
        registry.configure_instance(
            InstanceConfig("atomic_store", "idempotency", "probe")
        )
        assert atomic_store("idempotency") is not atomic_store("security")
    assert built == ["security", "idempotency"]


def test_an_absent_security_store_is_not_built_from_anything_else() -> None:
    registry = InfraRegistry()
    registry.set_config({})
    with use_infra(registry), pytest.raises(InstanceNotConfiguredError):
        atomic_store("security")


def test_test_isolation_defaults_to_the_options_unchanged() -> None:
    options = {"url": "redis://localhost/0"}
    assert InfraProvider.test_isolation(options, "gw3") is options


@pytest.mark.asyncio
async def test_isolation_identity_defaults_to_unknown() -> None:
    provider = _Notifier(name="default", driver="x", options={})
    assert await provider.isolation_identity() is None


def test_a_spec_s_feature_kinds_are_read_only() -> None:
    spec = NOTIFICATION_CAPABILITY
    with pytest.raises(TypeError):
        spec.feature_kinds["fax"] = "behaviour"  # type: ignore[index]
