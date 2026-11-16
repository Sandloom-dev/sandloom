"""Drivers read options, not settings; the kernel fills them (ADR-0016).

The ``redis`` cache, the ``redis`` / ``streams`` event buses and the
``celery`` task queue read no kernel setting: what they used to read
(``CACHE_URL``, ``BROKER_URL``, the ``CACHE_*`` client bounds) is put into
their instances' options by :func:`fill_driver_defaults`, as each driver's class
declares it (``InfraProvider.settings_fill``), so behaviour is unchanged. The
kernel's side of it names no driver.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
import sandloom_redis.cache as redis_cache
from sandloom_celery import taskqueue as celery_taskqueue
from sandloom_redis.cache import RedisCacheOptions

import sandloom.core.infra.config as infra_config
from sandloom.core.config.settings import Settings, settings
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.eventing.volatile_provider import VolatileBusOptions
from sandloom.core.infra.config import (
    DEFAULT_INSTANCE,
    FILLABLE_SETTINGS,
    InfraConfig,
    InstanceConfig,
    SettingsFill,
    fill_driver_defaults,
    load_infra_config,
)
from sandloom.core.infra.ports._base import InfraProvider
from sandloom.core.infra.registry import InfraRegistry
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.tests._driver_fill import fill, registry_for

_BOUNDS = {
    "socket_timeout_seconds": "cache_socket_timeout_seconds",
    "socket_connect_timeout_seconds": "cache_socket_connect_timeout_seconds",
    "health_check_interval_seconds": "cache_health_check_interval_seconds",
    "max_connections": "cache_max_connections",
    "pool_timeout_seconds": "cache_pool_timeout_seconds",
    "retry_attempts": "cache_retry_attempts",
}


def _cfg(capability: str, name: str, driver: str, **options: Any) -> InstanceConfig:
    return InstanceConfig(
        capability=capability, name=name, driver=driver, options=options
    )


def _loaded() -> InfraConfig:
    return fill(load_infra_config(settings))


def test_the_default_cache_gets_cache_url_and_every_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "cache_max_connections", 7)
    monkeypatch.setattr(settings, "cache_socket_timeout_seconds", 0.25)
    options = _loaded()["cache"][DEFAULT_INSTANCE].options
    assert options["url"] == settings.cache_url
    for option, setting in _BOUNDS.items():
        assert options[option] == getattr(settings, setting), option
    parsed = RedisCacheOptions.model_validate(options)  # the schema accepts it
    assert parsed.max_connections == 7
    assert parsed.socket_timeout_seconds == 0.25


def test_a_named_instance_gets_what_it_leaves_out_and_keeps_what_it_says() -> None:
    config = {
        "cache": {
            "sessions": _cfg("cache", "sessions", "redis", max_connections=3),
            "other": _cfg("cache", "other", "memory"),
        },
        "eventbus": {
            DEFAULT_INSTANCE: _cfg("eventbus", DEFAULT_INSTANCE, "streams"),
            "side": _cfg("eventbus", "side", "redis"),
            "mem": _cfg("eventbus", "mem", "memory"),
        },
        "taskqueue": {
            "jobs": _cfg("taskqueue", "jobs", "celery", broker="redis://b/2"),
        },
    }
    fill(config)
    sessions = config["cache"]["sessions"].options
    assert sessions["url"] == settings.cache_url
    assert sessions["max_connections"] == 3  # stated: kept
    assert config["cache"]["other"].options == {}  # not a redis cache
    default_bus = config["eventbus"][DEFAULT_INSTANCE].options
    assert default_bus["url"] == settings.cache_url
    side = config["eventbus"]["side"].options
    assert "url" not in side  # a named bus without one shares the default
    assert side["health_check_interval_seconds"] == (
        settings.cache_health_check_interval_seconds
    )
    VolatileBusOptions.model_validate(side)
    assert config["eventbus"]["mem"].options == {}
    jobs = config["taskqueue"]["jobs"].options
    assert jobs == {
        "broker": "redis://b/2",
        "backend": settings.cache_url,
        "health_timeout_seconds": settings.health_check_timeout_seconds,
    }
    celery_taskqueue.CeleryTaskQueueOptions.model_validate(jobs)


def test_the_default_taskqueue_gets_broker_backend_and_the_probe_bound() -> None:
    options = _loaded()["taskqueue"][DEFAULT_INSTANCE].options
    assert options == {
        "broker": settings.broker_url,
        "backend": settings.cache_url,
        "health_timeout_seconds": settings.health_check_timeout_seconds,
    }


def test_the_probe_bound_follows_the_setting_and_a_lower_stated_one_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "health_check_timeout_seconds", 5.0)
    config = {
        "taskqueue": {
            "a": _cfg("taskqueue", "a", "celery"),
            "b": _cfg("taskqueue", "b", "celery", health_timeout_seconds=1.0),
        }
    }
    fill(config)
    assert config["taskqueue"]["a"].options["health_timeout_seconds"] == 5.0
    assert config["taskqueue"]["b"].options["health_timeout_seconds"] == 1.0


def test_a_stated_probe_bound_never_exceeds_the_readiness_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/ready`` stops waiting at ``HEALTH_CHECK_TIMEOUT_SECONDS``: a connect
    allowed 300 s would only outlive its answered probe (option 300, setting 5:
    the probe is bounded by 5)."""
    monkeypatch.setattr(settings, "health_check_timeout_seconds", 5.0)
    config = {
        "taskqueue": {
            "b": _cfg("taskqueue", "b", "celery", health_timeout_seconds=300),
        }
    }
    fill(config)
    assert config["taskqueue"]["b"].options["health_timeout_seconds"] == 5.0
    options = config["taskqueue"]["b"].options
    provider = celery_taskqueue.create_celery_taskqueue(
        _cfg("taskqueue", "b", "celery", **{**options, "broker": "redis://b/2"})
    )
    assert provider._typed.health_timeout_seconds == 5.0


@pytest.mark.parametrize("raw", ["0", "-1", "301", "soon"])
def test_a_bad_health_check_timeout_is_refused_naming_the_variable(raw: str) -> None:
    from pydantic import ValidationError

    from sandloom.core.config.settings import Settings

    with pytest.raises(ValidationError, match="HEALTH_CHECK_TIMEOUT_SECONDS"):
        Settings(_env_file=None, HEALTH_CHECK_TIMEOUT_SECONDS=raw)


@pytest.mark.parametrize("bound", [0, -1, 301, "soon"])
def test_a_bad_probe_bound_is_refused_at_build(bound: object) -> None:
    with pytest.raises(ValueError, match="health_timeout_seconds"):
        celery_taskqueue.create_celery_taskqueue(
            _cfg(
                "taskqueue",
                "jobs",
                "celery",
                broker="redis://b/2",
                health_timeout_seconds=bound,
            )
        )


def test_a_redis_cache_built_from_options_ignores_the_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the driver connects to is the option, whatever CACHE_URL says."""
    built: list[RedisCacheOptions] = []

    class _Client:
        async def aclose(self) -> None:
            return None

    def _build(options: RedisCacheOptions) -> Any:
        built.append(options)
        return _Client()

    monkeypatch.setattr(redis_cache, "_build_client", _build)
    monkeypatch.setattr(settings, "cache_url", "redis://settings-host:1/0")
    provider = redis_cache.create_redis_cache(
        _cfg(
            "cache",
            "sessions",
            "redis",
            url="redis://option-host:2/3",
            max_connections=5,
        )
    )
    asyncio.run(provider._client_obj())
    assert built[0].url == "redis://option-host:2/3"
    assert built[0].max_connections == 5


def test_the_default_client_follows_the_default_instance_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default cache instance sets the default client's options; the
    default event bus never replaces them, and a change rebuilds the client."""
    built: list[RedisCacheOptions] = []

    class _Client:
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    def _build(options: RedisCacheOptions) -> Any:
        built.append(options)
        return _Client()

    monkeypatch.setattr(redis_cache, "_build_client", _build)
    monkeypatch.setattr(redis_cache, "_default_client", None)
    monkeypatch.setattr(redis_cache, "_default_client_origin", None)
    monkeypatch.setattr(redis_cache, "_default_options", None)
    monkeypatch.setattr(redis_cache, "_default_built_from", None)

    async def scenario() -> None:
        redis_cache.create_redis_cache(
            _cfg("cache", DEFAULT_INSTANCE, "redis", url="redis://a/0")
        )
        first = await redis_cache.get_default_client()
        redis_cache.configure_default_client(
            RedisCacheOptions(url="redis://bus/0"), replace=False
        )
        assert await redis_cache.get_default_client() is first
        redis_cache.create_redis_cache(
            _cfg("cache", DEFAULT_INSTANCE, "redis", url="redis://b/0")
        )
        second = await redis_cache.get_default_client()
        assert second is not first and first.closed
        await redis_cache.close_default_client()

    asyncio.run(scenario())
    assert [o.url for o in built] == ["redis://a/0", "redis://b/0"]


def test_an_unconfigured_default_client_says_why() -> None:
    async def build() -> None:
        await redis_cache.get_default_client()

    original = (redis_cache._default_client, redis_cache._default_options)
    redis_cache._default_client, redis_cache._default_options = None, None
    try:
        with pytest.raises(RuntimeError, match="configure_default_client"):
            asyncio.run(build())
    finally:
        redis_cache._default_client, redis_cache._default_options = original


def test_a_celery_instance_without_a_broker_is_refused() -> None:
    with pytest.raises(ValueError, match="'broker'"):
        celery_taskqueue.create_celery_taskqueue(_cfg("taskqueue", "jobs", "celery"))


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


def test_an_explicit_config_is_filled_alike_for_every_driver_incl_the_lease() -> None:
    """An explicit config asking for the fill gets the lease url and the cache
    url alike: one rule for every driver, not a kernel special case."""
    config: InfraConfig = {
        "lease": {DEFAULT_INSTANCE: _cfg("lease", DEFAULT_INSTANCE, "redis")},
        "cache": {DEFAULT_INSTANCE: _cfg("cache", DEFAULT_INSTANCE, "redis")},
    }
    build_infra_registry(
        InMemorySchemaRegistry(),
        settings=settings,
        config=config,
        fill_from_settings=True,
    )
    assert config["lease"][DEFAULT_INSTANCE].options["url"] == settings.cache_url
    assert config["cache"][DEFAULT_INSTANCE].options["url"] == settings.cache_url


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
        urls={"dsn": "cache_url"},
        bounds={"timeout": "cache_socket_timeout_seconds"},
        caps={"timeout": "health_check_timeout_seconds"},
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
            "empty": _cfg("probe", "empty", "known", dsn=""),
            "other": _cfg("probe", "other", "unregistered"),
        }
    }
    fill_driver_defaults(config, settings, _probe_registry().settings_fill)
    bare = config["probe"]["bare"].options
    assert bare == {
        "dsn": settings.cache_url,
        "timeout": min(
            settings.cache_socket_timeout_seconds, settings.health_check_timeout_seconds
        ),
    }
    assert config["probe"]["stated"].options == {"dsn": "x://own", "timeout": 2}
    assert config["probe"]["empty"].options["dsn"] == settings.cache_url  # falsy
    assert config["probe"]["other"].options == {}  # no class, no fill


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


def test_an_option_that_follows_reads_the_resolved_default_of_its_capability() -> None:
    def broadcast_url(cache: InstanceConfig | None) -> str | None:
        config: InfraConfig = {"broadcast": {"b": _cfg("broadcast", "b", "redis")}}
        if cache is not None:
            config["cache"] = {DEFAULT_INSTANCE: cache}
        fill(config)
        return config["broadcast"]["b"].options["url"]

    own = _cfg("cache", DEFAULT_INSTANCE, "redis", url="redis://own-host/9")
    assert broadcast_url(own) == "redis://own-host/9"  # INFRA's, not CACHE_URL
    assert broadcast_url(_cfg("cache", DEFAULT_INSTANCE, "redis")) == settings.cache_url
    # a cache of another driver has no url the broadcast could share
    assert (
        broadcast_url(_cfg("cache", DEFAULT_INSTANCE, "memory")) == settings.cache_url
    )
    assert broadcast_url(None) == settings.cache_url


def test_the_atomic_store_url_follows_the_cache_on_a_synthesized_instance_only() -> (
    None
):
    synthesized = _cfg("atomic_store", "security", "redis")
    synthesized.implicit = True
    declared = _cfg("atomic_store", "security2", "redis", max_connections=4)
    config = {
        "cache": {
            DEFAULT_INSTANCE: _cfg(
                "cache", DEFAULT_INSTANCE, "redis", url="redis://c/1"
            )
        },
        "atomic_store": {"security": synthesized, "security2": declared},
    }
    fill(config)
    assert synthesized.options["url"] == "redis://c/1"
    assert "url" not in declared.options  # its url is its own, never the cache's
    assert declared.options["max_connections"] == 4
    assert synthesized.options["max_connections"] == settings.cache_max_connections


def test_a_streams_bus_gets_the_url_on_the_default_instance_only() -> None:
    config = {
        "eventbus": {
            DEFAULT_INSTANCE: _cfg("eventbus", DEFAULT_INSTANCE, "streams"),
            "side": _cfg("eventbus", "side", "streams"),
        }
    }
    fill(config)
    assert config["eventbus"][DEFAULT_INSTANCE].options["url"] == settings.cache_url
    assert "url" not in config["eventbus"]["side"].options
    assert "socket_connect_timeout_seconds" in config["eventbus"]["side"].options


def test_a_settings_fill_refuses_a_declaration_that_cannot_work() -> None:
    with pytest.raises(ValueError, match="urls_for"):
        SettingsFill(urls={"url": "cache_url"}, urls_for="sometimes")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="follows needs urls"):
        SettingsFill(follows="cache")
    with pytest.raises(ValueError, match="option names to setting names"):
        SettingsFill(bounds={"timeout": 5})  # type: ignore[dict-item]


def test_a_settings_fill_takes_only_the_flat_infrastructure_settings() -> None:
    """A secret or a typo is refused where the declaration is built, in each of
    the three tables, not silently copied (or left unfilled) at startup."""
    with pytest.raises(ValueError, match="jwt_secret"):
        SettingsFill(urls={"url": "jwt_secret"})
    with pytest.raises(ValueError, match="cache_urll"):
        SettingsFill(bounds={"url": "cache_urll"})
    with pytest.raises(ValueError, match="database_url"):
        SettingsFill(caps={"timeout": "database_url"})
    SettingsFill(
        urls={"a": "cache_url", "b": "broker_url"},
        bounds={"c": "cache_max_connections"},
        caps={"d": "health_check_timeout_seconds"},
    )  # the allowed ones stay allowed


_DECLARING_DRIVERS = [
    ("cache", "redis"),
    ("broadcast", "redis"),
    ("atomic_store", "redis"),
    ("lease", "redis"),
    ("eventbus", "streams"),
    ("eventbus", "redis"),
    ("taskqueue", "celery"),
]


@pytest.mark.parametrize(("capability", "driver"), _DECLARING_DRIVERS)
def test_every_declared_fill_names_settings_that_exist(
    capability: str, driver: str
) -> None:
    config: InfraConfig = {
        capability: {DEFAULT_INSTANCE: _cfg(capability, DEFAULT_INSTANCE, driver)}
    }
    declared = registry_for(config).settings_fill(capability, driver)
    assert declared is not None, (capability, driver)
    named = {
        *declared.urls.values(),
        *declared.bounds.values(),
        *declared.caps.values(),
    }
    assert named, (capability, driver)
    assert named <= set(Settings.model_fields), named - set(Settings.model_fields)
    assert named <= FILLABLE_SETTINGS


def test_a_settings_fill_cannot_be_changed_after_it_is_declared() -> None:
    table = {"dsn": "cache_url"}
    declared = SettingsFill(urls=table)
    table["other"] = "broker_url"
    assert dict(declared.urls) == {"dsn": "cache_url"}
    with pytest.raises(TypeError):
        declared.urls["other"] = "broker_url"  # type: ignore[index]


# -- when the registry fills ---------------------------------------------------


def _redis_cache_config() -> InfraConfig:
    return {
        "cache": {
            DEFAULT_INSTANCE: _cfg(
                "cache", DEFAULT_INSTANCE, "redis", url="redis://x/0"
            )
        }
    }


def test_an_explicit_config_is_built_as_given_unless_the_caller_asks() -> None:
    given = build_infra_registry(InMemorySchemaRegistry(), config=_redis_cache_config())
    options = given.instance_config("cache", DEFAULT_INSTANCE)
    assert options is not None and "max_connections" not in options.options
    asked = build_infra_registry(
        InMemorySchemaRegistry(), config=_redis_cache_config(), fill_from_settings=True
    )
    options = asked.instance_config("cache", DEFAULT_INSTANCE)
    assert options is not None
    assert options.options["url"] == "redis://x/0"  # stated: kept
    assert options.options["max_connections"] == settings.cache_max_connections


def test_a_registry_built_from_the_settings_fills_what_drivers_declare() -> None:
    registry = build_infra_registry(InMemorySchemaRegistry())
    queue = registry.instance_config("taskqueue", DEFAULT_INSTANCE)
    assert queue is not None
    assert queue.options["broker"] == settings.broker_url
    assert queue.options["health_timeout_seconds"] == (
        settings.health_check_timeout_seconds
    )


def test_loading_the_config_imports_no_driver_and_fills_nothing() -> None:
    config = load_infra_config(settings)
    assert config["cache"][DEFAULT_INSTANCE].options == {}
    queue = config["taskqueue"][DEFAULT_INSTANCE].options
    assert "health_timeout_seconds" not in queue  # a driver's fill, not the kernel's


# -- the retention a deployment's bus is configured with -------------------------


def _bus_settings(backend: str, **options: Any) -> Any:
    infra = {"eventbus": {DEFAULT_INSTANCE: {"driver": backend, **options}}}
    return SimpleNamespace(event_bus_backend=None, infra=infra)


def test_the_retention_is_the_stated_option_else_the_drivers_schema_default() -> None:
    from sandloom.core.config.eventing import DEFAULT_EVENT_STREAM_RETENTION_SECONDS
    from sandloom.core.infra.registry import configured_stream_retention_seconds

    assert configured_stream_retention_seconds(_bus_settings("streams")) == (
        DEFAULT_EVENT_STREAM_RETENTION_SECONDS
    )
    assert (
        configured_stream_retention_seconds(
            _bus_settings("streams", retention_seconds=60)
        )
        == 60
    )
    assert (
        configured_stream_retention_seconds(
            _bus_settings("streams", retention_seconds=0)
        )
        == 0
    )


@pytest.mark.parametrize("driver", ["memory", "redis", "not_installed_here"])
def test_a_driver_without_a_retention_option_has_none_configured(driver: str) -> None:
    from sandloom.core.infra.registry import configured_stream_retention_seconds

    assert configured_stream_retention_seconds(_bus_settings(driver)) is None
    assert (
        configured_stream_retention_seconds(_bus_settings(driver, retention_seconds=9))
        == 9  # only what the configuration states: no class to refute it
    )


@pytest.mark.parametrize("bad", [-1, True, "7", 1.5])
def test_a_retention_that_is_not_a_count_of_seconds_is_not_determined(
    bad: object,
) -> None:
    from sandloom.core.infra.registry import configured_stream_retention_seconds

    settings_ = _bus_settings("streams", retention_seconds=bad)
    assert configured_stream_retention_seconds(settings_) is None
