"""The kernel names no driver: the composition selects them (spec 1, item 45).

A process whose composition and ``INFRA`` select nothing for a capability it
needs refuses to start (SANDLOOM-B037) in every environment and never falls
back to ``memory``. The Redis and Celery drivers come from their packages'
entry points; the kernel knows only the packages' names, for the install hint,
and hands the ``atomic_store`` and ``lease`` drivers its eviction policy.
"""

from __future__ import annotations

from importlib import metadata
from typing import Any

import pytest

import sandloom.core.infra.config as config_module
from sandloom.core.composition import root_default_infra
from sandloom.core.config.common import Environment
from sandloom.core.config.settings import settings
from sandloom.core.drivers.bootstrap import (
    BUILTIN_DRIVER_ROWS,
    KNOWN_DRIVER_PACKAGES,
    build_infra_registry,
    unverified_noeviction_allowed,
)
from sandloom.core.infra.config import (
    KERNEL_INSTANCES,
    REQUIRED_AT_STARTUP,
    InfraConfigError,
    InstanceConfig,
    load_infra_config,
    use_default_infra,
)
from sandloom.core.infra.registry import (
    DriverNotRegisteredError,
    DriverUnavailableError,
    InfraRegistry,
)
from sandloom.core.schema.registry import InMemorySchemaRegistry

_STANDARD = {
    "database": {"default": {"driver": "sqlalchemy"}},
    "cache": {"default": {"driver": "redis"}},
    "eventbus": {"default": {"driver": "streams"}},
    "atomic_store": {"security": {"driver": "redis"}},
}


def _no_bus_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "event_bus_backend", None)
    monkeypatch.setattr(settings, "infra", {})


# ------------------------------------------------- no composition: B037 ----


@pytest.mark.parametrize("environment", [Environment.dev, Environment.prod])
def test_no_selection_refuses_startup_with_b037_in_every_environment(
    monkeypatch: pytest.MonkeyPatch, environment: Environment
) -> None:
    _no_bus_selector(monkeypatch)
    monkeypatch.setattr(settings, "environment", environment)
    use_default_infra(None)
    with pytest.raises(DriverUnavailableError) as caught:
        build_infra_registry(InMemorySchemaRegistry(), load_plugins=False)
    assert caught.value.sandloom_code == "SANDLOOM-B037"
    message = str(caught.value)
    for capability, name in sorted(REQUIRED_AT_STARTUP):
        assert f"{capability}/{name}: no driver is selected" in message
    assert "sandloom[standard]" in message
    assert "INFRA" in message


def test_memory_is_never_the_fallback_for_the_event_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_bus_selector(monkeypatch)
    use_default_infra(None)
    assert "eventbus" not in load_infra_config(settings)
    with pytest.raises(DriverUnavailableError, match=r"eventbus/default"):
        build_infra_registry(InMemorySchemaRegistry(), load_plugins=False)


def test_a_selection_for_every_required_instance_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_bus_selector(monkeypatch)
    use_default_infra(_STANDARD)
    registry = build_infra_registry(InMemorySchemaRegistry())
    for capability, name in KERNEL_INSTANCES:
        assert registry.in_use(capability, name)
    # The package's entry points registered the drivers the composition named.
    assert registry.driver_class("atomic_store", "redis").__name__ == (
        "RedisAtomicStoreProvider"
    )


def test_an_explicit_config_is_built_as_given_not_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool process or a test passes its own config: no instance is demanded."""
    _no_bus_selector(monkeypatch)
    use_default_infra(None)
    build_infra_registry(InMemorySchemaRegistry(), config={}, load_plugins=False)


# --------------------------------------------- selection and its overrides ----


def test_infra_overrides_the_composition_default_and_names_it_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_bus_selector(monkeypatch)
    use_default_infra(_STANDARD)
    default = load_infra_config(settings)["cache"]["default"]
    assert (default.driver, default.implicit) == ("redis", True)
    monkeypatch.setattr(settings, "infra", {"cache": {"default": {"driver": "memory"}}})
    named = load_infra_config(settings)["cache"]["default"]
    assert (named.driver, named.implicit) == ("memory", False)


def test_event_bus_backend_selects_the_bus_and_unset_leaves_the_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_bus_selector(monkeypatch)
    use_default_infra(_STANDARD)
    assert load_infra_config(settings)["eventbus"]["default"].driver == "streams"
    monkeypatch.setattr(settings, "event_bus_backend", "memory")
    assert load_infra_config(settings)["eventbus"]["default"].driver == "memory"


_BASE_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/db",
    "CACHE_URL": "redis://localhost:6379/0",
    "BROKER_URL": "redis://localhost:6379/1",
    "JWT_SECRET": "kb09-test-secret-0123456789abcdef0123456789abcdef",
}


def _built_settings(**env: str) -> Any:
    from sandloom.core.config.settings import Settings

    return Settings(_env_file=None, **{**_BASE_ENV, **env})  # type: ignore[call-arg]


@pytest.mark.parametrize("value", ["auto", "AUTO", " auto "])
def test_the_auto_literal_is_refused_naming_the_fix(value: str) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="unset EVENT_BUS_BACKEND"):
        _built_settings(EVENT_BUS_BACKEND=value)


def test_the_event_bus_backend_is_none_when_unset_and_a_driver_otherwise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EVENT_BUS_BACKEND", raising=False)
    assert _built_settings().event_bus_backend is None
    assert _built_settings(EVENT_BUS_BACKEND="streams").event_bus_backend == "streams"


def test_another_unknown_event_bus_backend_is_still_refused() -> None:
    """The refusal of ``auto`` names the fix; any other bad value stays the
    plain Literal error (an allow-list, not a deny-list of one word)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as caught:
        _built_settings(EVENT_BUS_BACKEND="kafka")
    assert "unset EVENT_BUS_BACKEND" not in str(caught.value)


def test_a_malformed_composition_selection_is_refused() -> None:
    with pytest.raises(InfraConfigError):
        use_default_infra({"cache": {"default": {"options": {}}}})


def test_the_production_security_store_is_never_built_from_a_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_bus_selector(monkeypatch)
    use_default_infra(_STANDARD)
    monkeypatch.setattr(settings, "environment", Environment.dev)
    assert "security" in load_infra_config(settings)["atomic_store"]
    monkeypatch.setattr(settings, "environment", Environment.prod)
    assert "security" not in load_infra_config(settings).get("atomic_store", {})
    monkeypatch.setattr(
        settings, "infra", {"atomic_store": {"security": {"driver": "redis"}}}
    )
    named = load_infra_config(settings)["atomic_store"]["security"]
    assert named.implicit is False


def test_a_process_that_booted_no_app_adopts_the_roots_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sandloom migrate`` and the backup tools never run ``create_app``: they
    read the selection from the composition root's package."""
    pytest.importorskip("sandloom_standard")  # absent in the kernel-only venv
    _no_bus_selector(monkeypatch)
    monkeypatch.setattr(config_module, "_COMPOSITION_ADOPTED", False)
    monkeypatch.setenv("SANDLOOM_COMPOSITION_ROOT", "sandloom_standard.main")
    assert load_infra_config(settings)["cache"]["default"].driver == "redis"
    monkeypatch.setattr(config_module, "_COMPOSITION_ADOPTED", False)
    monkeypatch.setenv("SANDLOOM_COMPOSITION_ROOT", "sandloom.main")
    assert "cache" not in load_infra_config(settings)


def test_the_standard_composition_exports_its_selection() -> None:
    pytest.importorskip("sandloom_standard")  # absent in the kernel-only venv
    selection = root_default_infra("sandloom_standard.main")
    assert selection is not None
    assert selection["database"]["default"]["driver"] == "sqlalchemy"
    assert selection["lease"]["beat"]["driver"] == "redis"
    assert root_default_infra("sandloom.main") is None


# ----------------------------------------------------- the policy hook ----


def test_entry_point_atomic_store_and_lease_get_the_eviction_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_bus_selector(monkeypatch)
    use_default_infra(_STANDARD)
    config = {
        "atomic_store": {
            "security": InstanceConfig(
                "atomic_store", "security", "redis", {"url": "redis://h:1/0"}
            )
        },
        "lease": {
            "beat": InstanceConfig("lease", "beat", "redis", {"url": "redis://h:1/0"})
        },
    }
    registry = build_infra_registry(InMemorySchemaRegistry(), config=config)
    for capability, name in (("atomic_store", "security"), ("lease", "beat")):
        built: Any = registry.provider(capability, name)
        assert built._assume_allowed is not None, capability
        # dev: the policy allows ``assume``; prod: only the accepted risk does.
        assert built._assume_allowed() is unverified_noeviction_allowed(settings)
        assert built._assume_allowed() is True
    monkeypatch.setattr(settings, "environment", Environment.prod)
    assert built._assume_allowed() is False


def test_a_class_without_the_parameter_is_built_by_from_config() -> None:
    built: list[str] = []

    class Plain:
        def __init__(self, name: str, driver: str, options: dict[str, Any]) -> None:
            built.append("init")

        @classmethod
        def from_config(cls, cfg: InstanceConfig) -> Plain:
            built.append("from_config")
            return cls(cfg.name, cfg.driver, dict(cfg.options))

    class Hooked:
        def __init__(
            self, name: str, driver: str, options: dict[str, Any], assume_allowed: Any
        ) -> None:
            self.assume_allowed = assume_allowed

        @classmethod
        def from_config(cls, cfg: InstanceConfig) -> Hooked:
            raise AssertionError("the constructor with the policy builds it")

    registry = InfraRegistry()
    registry.set_policy("cache", assume_allowed=lambda: True)
    cfg = InstanceConfig("cache", "default", "x", {})
    factory: Any = registry._class_factory
    factory("cache", Plain)(cfg)
    assert built == ["from_config", "init"]
    hooked = factory("cache", Hooked)(cfg)
    assert hooked.assume_allowed() is True


# --------------------------------------------------- the install hint ----


def _without_the_package(monkeypatch: pytest.MonkeyPatch, package: str) -> None:
    original = InfraRegistry._is_installed

    def is_installed(self: InfraRegistry, name: str) -> bool:
        return False if name == package else original(self, name)

    monkeypatch.setattr(InfraRegistry, "_is_installed", is_installed)


@pytest.mark.parametrize(
    ("capability", "driver", "package", "install"),
    [
        ("cache", "redis", "sandloom_redis", "pip install sandloom-redis"),
        ("lease", "redis", "sandloom_redis", "pip install sandloom-redis"),
        ("eventbus", "streams", "sandloom_redis", "pip install sandloom-redis"),
        ("taskqueue", "celery", "sandloom_celery", "pip install sandloom-celery"),
    ],
)
def test_a_named_driver_whose_package_is_missing_refuses_with_the_install_hint(
    monkeypatch: pytest.MonkeyPatch,
    capability: str,
    driver: str,
    package: str,
    install: str,
) -> None:
    _without_the_package(monkeypatch, package)
    config = {
        capability: {
            "default": InstanceConfig(capability, "default", driver, {"url": "x"})
        }
    }
    with pytest.raises(DriverUnavailableError) as caught:
        build_infra_registry(
            InMemorySchemaRegistry(), config=config, load_plugins=False
        )
    assert caught.value.sandloom_code == "SANDLOOM-B037"
    assert caught.value.missing == (package,)
    assert install in str(caught.value)
    assert f"needs the package {package!r}" in str(caught.value)


def test_an_unknown_driver_is_not_an_installation_fact() -> None:
    config = {"cache": {"default": InstanceConfig("cache", "default", "nosuch", {})}}
    with pytest.raises(DriverNotRegisteredError) as caught:
        build_infra_registry(
            InMemorySchemaRegistry(), config=config, load_plugins=False
        )
    assert not isinstance(caught.value, DriverUnavailableError)


def test_a_package_name_must_be_an_import_name() -> None:
    with pytest.raises(ValueError, match="import name"):
        InfraRegistry().register_driver_package(
            "cache", "x", package="sandloom-redis", install="pip install x"
        )


def test_the_kernel_keeps_no_row_for_a_driver_package() -> None:
    """The rows are the kernel's own drivers: nothing it ships from a package."""
    for _capability, _driver, target, *_rest in BUILTIN_DRIVER_ROWS:
        assert not target.startswith(("sandloom_redis", "sandloom_celery")), target


def test_the_known_packages_are_the_entry_points_the_packages_publish() -> None:
    entries = {
        entry.name: entry.dist.name
        for entry in metadata.entry_points(group="sandloom.drivers")
        if entry.dist is not None
        and entry.dist.name in {"sandloom-redis", "sandloom-celery"}
    }
    for (capability, driver), (package, install) in KNOWN_DRIVER_PACKAGES.items():
        dist = entries[f"{capability}.{driver}"]
        assert package == dist.replace("-", "_")
        assert install == f"pip install {dist}"
