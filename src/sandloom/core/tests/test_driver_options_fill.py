"""Drivers read options, not settings; the kernel fills them (ADR-0016): the
engine, with no driver installed.

The one thing a driver takes from the settings is the readiness probe's bound
(``HEALTH_CHECK_TIMEOUT_SECONDS``): :func:`fill_driver_defaults` puts it into
its instances' options as each driver's class declares it
(``InfraProvider.settings_fill``). No url and no client bound is filled: the
flat ``CACHE_URL`` / ``BROKER_URL`` / ``CACHE_*`` settings are removed
(``tiers.REMOVED``). The kernel's side names no driver and runs on a driver of
its own (``_Probe``): these tests pass where neither ``sandloom-redis`` nor
``sandloom-celery`` is installed. What each driver declares is held where it
lives: ``packages/sandloom-redis/tests/test_redis_options_from_settings.py`` and
``packages/sandloom-celery/tests/test_celery_options_from_settings.py``.
"""

from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

import sandloom.core.infra.config as infra_config
import sandloom.core.infra.registry as registry_module
from sandloom.core.config.settings import Settings, settings
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.infra.config import (
    DEFAULT_INSTANCE,
    InstanceConfig,
    SettingsFill,
    fill_driver_defaults,
    load_infra_config,
)
from sandloom.core.infra.ports._base import InfraProvider
from sandloom.core.infra.registry import InfraRegistry
from sandloom.core.schema.registry import InMemorySchemaRegistry


def _cfg(capability: str, name: str, driver: str, **options: Any) -> InstanceConfig:
    return InstanceConfig(
        capability=capability, name=name, driver=driver, options=options
    )


@pytest.mark.parametrize("raw", ["0", "-1", "301", "soon"])
def test_a_bad_health_check_timeout_is_refused_naming_the_variable(raw: str) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="HEALTH_CHECK_TIMEOUT_SECONDS"):
        Settings(_env_file=None, HEALTH_CHECK_TIMEOUT_SECONDS=raw)


# -- the kernel's side names no driver ----------------------------------------


def test_the_kernels_fill_names_no_driver() -> None:
    """``sandloom.core.infra.config`` holds no ``redis`` / ``streams`` /
    ``celery`` string outside a docstring (spec 13 item 45): a driver's
    declaration, not a kernel table, says what it takes from the settings."""
    tree = ast.parse(inspect.getsource(infra_config))
    strings = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert not strings & {"redis", "streams", "celery"}


def test_the_registry_build_names_no_driver_in_its_fill() -> None:
    """``build_infra_registry`` (bootstrap.py) fills through the registry's
    lookup: no driver name, no ``cache_url`` in the function (the beat lease's
    url once was filled there by name)."""
    import sandloom.core.drivers.bootstrap as bootstrap

    tree = ast.parse(inspect.getsource(bootstrap))
    builder = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "build_infra_registry"
    )
    strings = {
        node.value
        for node in ast.walk(builder)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert not strings & {"redis", "streams", "celery", "cache_url", "lease"}
    functions = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    assert not {name for name in functions if "lease" in name}


def test_an_empty_explicit_config_is_built_as_given_not_loaded_from_settings() -> None:
    registry = build_infra_registry(
        InMemorySchemaRegistry(), settings=settings, config={}
    )
    assert registry.instances("cache") == []


class _Probe(InfraProvider):
    """A driver the kernel has never heard of."""

    capability: ClassVar[str] = "probe"
    contract_version = 1
    settings_fill = SettingsFill(
        bounds={"timeout": "health_check_timeout_seconds"},
        caps={"cap": "health_check_timeout_seconds"},
    )


def _probe_registry(**kwargs: Any) -> InfraRegistry:
    registry = InfraRegistry()
    registry.register_driver("probe", "known", _Probe, **kwargs)
    return registry


def test_a_driver_the_kernel_never_heard_of_is_filled_as_it_declares() -> None:
    config = {
        "probe": {
            "bare": _cfg("probe", "bare", "known"),
            "stated": _cfg("probe", "stated", "known", dsn="x://own", timeout=2),
            "capped": _cfg("probe", "capped", "known", cap=10_000),
            "other": _cfg("probe", "other", "unregistered"),
        }
    }
    fill_driver_defaults(config, settings, _probe_registry().settings_fill)
    bound = settings.health_check_timeout_seconds
    # the bound only; no url is invented for an instance that states none
    assert config["probe"]["bare"].options == {"timeout": bound}
    assert config["probe"]["stated"].options == {"dsn": "x://own", "timeout": 2}
    capped = config["probe"]["capped"].options
    assert capped["cap"] == bound  # a probe never waits longer than the bound
    assert config["probe"]["other"].options == {}  # no class, no fill


def test_no_instance_takes_an_option_from_another_instance() -> None:
    """An instance's url is its own (``INFRA``): nothing fills one from a
    setting, and none from another instance, in its capability or another."""
    config = {
        "probe": {
            DEFAULT_INSTANCE: _cfg("probe", DEFAULT_INSTANCE, "known", dsn="x://a"),
            "named": _cfg("probe", "named", "known"),
        },
        "other": {"b": _cfg("other", "b", "known")},
    }
    fill_driver_defaults(config, settings, lambda _cap, _driver: _Probe.settings_fill)
    assert "dsn" not in config["probe"]["named"].options
    assert "dsn" not in config["other"]["b"].options
    assert config["probe"][DEFAULT_INSTANCE].options["dsn"] == "x://a"


@pytest.mark.parametrize("field", ["urls", "urls_for", "follows"])
def test_a_settings_fill_takes_no_url_table_and_no_follows(field: str) -> None:
    """The url fill and the cross-capability inheritance are gone: the fields
    are not accepted (a driver that still declares one fails at import)."""
    with pytest.raises(TypeError, match=field):
        SettingsFill(**{field: {"dsn": "cache_url"}})  # type: ignore[arg-type]


def test_a_driver_whose_package_is_missing_is_not_filled_nor_imported() -> None:
    registry = _probe_registry(requires=("sandloom_no_such_package_xyz",))
    assert registry.settings_fill("probe", "known") is None
    config = {"probe": {"a": _cfg("probe", "a", "known")}}
    fill_driver_defaults(config, settings, registry.settings_fill)
    assert config["probe"]["a"].options == {}


def test_a_lazy_row_is_not_imported_when_its_package_is_missing() -> None:
    registry = InfraRegistry()
    registry.register_driver(
        "probe",
        "lazy",
        "sandloom_no_such_package_xyz.driver:Nothing",
        requires=("sandloom_no_such_package_xyz",),
    )
    assert registry.settings_fill("probe", "lazy") is None  # no ImportError


def test_a_broken_lazy_row_still_raises_when_its_package_is_there() -> None:
    from sandloom.core.infra.registry import DriverLoadError

    registry = InfraRegistry()
    registry.register_driver("probe", "lazy", "sandloom_no_such_module_xyz:Nothing")
    with pytest.raises(DriverLoadError):
        registry.settings_fill("probe", "lazy")


def test_a_settings_fill_refuses_a_declaration_that_cannot_work() -> None:
    with pytest.raises(ValueError, match="option names to setting names"):
        SettingsFill(bounds={"timeout": 5})  # type: ignore[dict-item]


def test_a_settings_fill_takes_only_the_health_check_timeout() -> None:
    """A secret, a typo or a removed flat setting is refused where the
    declaration is built, in each table, not silently copied (or left unfilled)
    at startup."""
    with pytest.raises(ValueError, match="jwt_secret"):
        SettingsFill(bounds={"url": "jwt_secret"})
    with pytest.raises(ValueError, match="cache_url"):
        SettingsFill(bounds={"url": "cache_url"})
    with pytest.raises(ValueError, match="broker_url"):
        SettingsFill(caps={"timeout": "broker_url"})
    with pytest.raises(ValueError, match="cache_max_connections"):
        SettingsFill(bounds={"c": "cache_max_connections"})
    with pytest.raises(ValueError, match="database_url"):
        SettingsFill(caps={"timeout": "database_url"})
    SettingsFill(
        bounds={"c": "health_check_timeout_seconds"},
        caps={"d": "health_check_timeout_seconds"},
    )  # the allowed one stays allowed


def test_a_settings_fill_cannot_be_changed_after_it_is_declared() -> None:
    table = {"timeout": "health_check_timeout_seconds"}
    declared = SettingsFill(bounds=table)
    table["other"] = "health_check_timeout_seconds"
    assert dict(declared.bounds) == {"timeout": "health_check_timeout_seconds"}
    with pytest.raises(TypeError):
        declared.bounds["other"] = "health_check_timeout_seconds"  # type: ignore[index]


# -- when the registry fills ---------------------------------------------------


def test_loading_the_config_imports_no_driver_and_fills_nothing() -> None:
    config = load_infra_config(settings)
    stated = settings.infra["cache"][DEFAULT_INSTANCE]  # what INFRA says, no more
    assert config["cache"][DEFAULT_INSTANCE].options == {
        key: value for key, value in stated.items() if key != "driver"
    }
    queue = config["taskqueue"][DEFAULT_INSTANCE].options
    assert "health_timeout_seconds" not in queue  # a driver's fill, not the kernel's


# -- the retention a deployment's bus is configured with -------------------------


def _bus_settings(backend: str, **options: Any) -> Any:
    infra = {"eventbus": {DEFAULT_INSTANCE: {"driver": backend, **options}}}
    return SimpleNamespace(event_bus_backend=None, infra=infra)


@pytest.mark.parametrize("driver", ["memory", "not_installed_here"])
def test_a_driver_without_a_retention_option_has_none_configured(driver: str) -> None:
    from sandloom.core.infra.registry import configured_stream_retention_seconds

    assert configured_stream_retention_seconds(_bus_settings(driver)) is None
    assert (
        configured_stream_retention_seconds(_bus_settings(driver, retention_seconds=9))
        == 9  # only what the configuration states: no class to refute it
    )


def _without_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    """A process where no driver package publishes an entry point."""
    monkeypatch.setattr(registry_module, "_one_plugin", lambda group, name: None)


def test_a_missing_driver_package_is_named_with_its_install_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _without_plugins(monkeypatch)
    found = registry_module.configured_stream_retention(_bus_settings("streams"))
    assert found.seconds is None
    assert found.reason is not None
    assert "not installed" in found.reason
    assert "pip install sandloom-redis" in found.reason
    assert "EVENT_BUS_BACKEND" not in found.reason  # the setting is not the fault
    # what the configuration states is still what it states
    stated = _bus_settings("streams", retention_seconds=60)
    assert registry_module.configured_stream_retention(stated).seconds == 60


def test_a_driver_that_fails_to_load_is_a_reason_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(capability: str, driver: str) -> None:
        raise registry_module.DriverLoadError("boom: broken import")

    monkeypatch.setattr(registry_module, "_declared_class", broken)
    found = registry_module.configured_stream_retention(_bus_settings("streams"))
    assert found.seconds is None
    assert found.reason is not None
    assert "cannot be loaded" in found.reason and "boom" in found.reason


def test_a_failure_that_is_not_a_plugin_error_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(capability: str, driver: str) -> None:
        raise RuntimeError("not a driver error")

    monkeypatch.setattr(registry_module, "_declared_class", unexpected)
    with pytest.raises(RuntimeError, match="not a driver error"):
        registry_module.configured_stream_retention(_bus_settings("streams"))


def test_a_volatile_bus_and_an_unconfigured_instance_say_why() -> None:
    volatile = registry_module.configured_stream_retention(_bus_settings("memory"))
    assert volatile.reason is not None
    assert "no retention_seconds option" in volatile.reason
    assert "EVENT_BUS_BACKEND=streams" in volatile.reason
    absent = registry_module.configured_stream_retention(
        _bus_settings("streams"), "nope"
    )
    assert absent.seconds is None and absent.reason is not None
    assert "'nope'" in absent.reason


@pytest.mark.parametrize("bad", [-1, True, "7", 1.5])
def test_a_retention_that_is_not_a_count_of_seconds_is_not_determined(
    bad: object,
) -> None:
    from sandloom.core.infra.registry import configured_stream_retention_seconds

    settings_ = _bus_settings("streams", retention_seconds=bad)
    assert configured_stream_retention_seconds(settings_) is None


def test_the_purge_refusal_carries_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    from sandloom.core.projections.operations import _stream_retention_s
    from sandloom.core.projections.purge import PurgeRefused

    _without_plugins(monkeypatch)
    core = SimpleNamespace(settings=_bus_settings("streams"))
    with pytest.raises(PurgeRefused, match="pip install sandloom-redis"):
        _stream_retention_s(core)
    assert _stream_retention_s(core, force=True) == 1.0


class _UrlDriver(InfraProvider):
    contract_version = 1
    url_option = "dsn"


class _NoUrlDriver(InfraProvider):
    contract_version = 1


def test_url_options_are_what_the_installed_drivers_declare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    classes = {("cache", "valkey"): _UrlDriver, ("objectstore", "local"): _NoUrlDriver}
    monkeypatch.setattr(
        registry_module,
        "_declared_class",
        lambda cap, driver: classes.get((cap, driver)),
    )
    selection = {
        "cache": {"default": {"driver": "valkey"}, "other": {"driver": "valkey"}},
        "objectstore": {"default": {"driver": "local"}},
        # no package publishes it (a kernel driver or one not installed)
        "database": {"default": {"driver": "sqlalchemy"}},
        # an instance that names no driver keeps the composition's: nothing to read
        "lease": {"beat": {}},
    }
    assert registry_module.declared_url_options(selection) == {"cache.valkey": "dsn"}


def test_a_driver_that_cannot_load_stops_the_url_option_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(capability: str, driver: str) -> None:
        raise registry_module.DriverLoadError("boom: broken import")

    monkeypatch.setattr(registry_module, "_declared_class", broken)
    selection = {"cache": {"default": {"driver": "redis"}}}
    with pytest.raises(registry_module.DriverLoadError, match="boom"):
        registry_module.declared_url_options(selection)


def test_an_instance_that_is_not_a_mapping_is_refused_by_the_url_option_read() -> None:
    with pytest.raises(TypeError, match=r"cache\.default"):
        registry_module.declared_url_options({"cache": {"default": "redis"}})  # type: ignore[dict-item]
