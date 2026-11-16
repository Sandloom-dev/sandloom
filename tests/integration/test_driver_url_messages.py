"""A Redis or Celery instance with no url refuses startup and names its INFRA option.

``CACHE_URL`` and ``BROKER_URL`` are removed settings and nothing is inherited
(ADR-0016): an instance whose INFRA states no ``url`` / ``broker`` is a
configuration mistake, so startup aborts in every environment, and the message
says which INFRA option to set, never that the kernel fills it from a flat
setting.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sandloom_celery.taskqueue import CeleryTaskQueueProvider, MissingBrokerError
from sandloom_redis import cache as redis_cache
from sandloom_redis._url import MissingUrlError
from sandloom_redis.cache import RedisCacheProvider

from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.infra.config import InfraConfig, InstanceConfig
from sandloom.core.infra.registry import InfraRegistry, InfraStartupError
from sandloom.core.schema.registry import InMemorySchemaRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["default", "sessions"])
async def test_a_redis_cache_without_a_url_names_the_infra_option(name: str) -> None:
    cfg = InstanceConfig("cache", name, "redis", {})
    provider = RedisCacheProvider.from_config(cfg)  # building opens nothing
    with pytest.raises(MissingUrlError) as caught:
        await provider.aopen()
    message = str(caught.value)
    assert f"cache.{name}.url" in message
    assert "INFRA=" in message
    assert "CACHE_URL" not in message
    assert caught.value.fatal_at_startup is True


@pytest.mark.asyncio
async def test_a_redis_cache_with_a_url_opens_without_connecting() -> None:
    provider = RedisCacheProvider.from_config(
        InstanceConfig("cache", "sessions", "redis", {"url": "redis://x:6379/0"})
    )
    await provider.aopen()  # no connection: the host above does not exist
    assert provider.name == "sessions"


@pytest.mark.parametrize("name", ["default", "jobs"])
def test_a_celery_taskqueue_without_a_broker_names_the_infra_option(name: str) -> None:
    cfg = InstanceConfig("taskqueue", name, "celery", {})
    with pytest.raises(MissingBrokerError) as caught:
        CeleryTaskQueueProvider.from_config(cfg)
    message = str(caught.value)
    assert f"taskqueue.{name}.broker" in message
    assert "INFRA=" in message
    assert "BROKER_URL" not in message
    assert caught.value.fatal_at_startup is True


@pytest.mark.asyncio
async def test_startup_aborts_on_a_redis_cache_with_no_url_even_outside_production() -> (
    None
):
    config: InfraConfig = {
        "cache": {"default": InstanceConfig("cache", "default", "redis", {})}
    }
    registry = build_infra_registry(
        InMemorySchemaRegistry(), config=config, load_plugins=True
    )
    # not strict (the non-production default): a plain failure would only warn
    with pytest.raises(InfraStartupError, match=r"cache\.default\.url"):
        await registry.aopen_all(strict=False)


# -- every Redis driver, through the registry's startup ------------------------

_REDIS_DRIVERS = [
    ("cache", "redis"),
    ("atomic_store", "redis"),
    ("broadcast", "redis"),
    ("lease", "redis"),
    ("eventbus", "streams"),
]


@pytest.fixture
def _no_default_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """The process-wide default client unset: a url-less ``streams`` bus has
    nothing to fall back on."""
    monkeypatch.setattr(redis_cache, "_default_options", None)
    monkeypatch.setattr(redis_cache, "_default_client", None)


def _registry(
    capability: str, name: str, driver: str, options: dict[str, str]
) -> InfraRegistry:
    config: InfraConfig = {
        capability: {name: InstanceConfig(capability, name, driver, options)}
    }
    return build_infra_registry(
        InMemorySchemaRegistry(), config=config, load_plugins=True
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_default_client")
@pytest.mark.parametrize(("capability", "driver"), _REDIS_DRIVERS)
@pytest.mark.parametrize("name", ["default", "other"])
async def test_every_redis_driver_refuses_a_url_less_instance_at_startup(
    capability: str, driver: str, name: str
) -> None:
    """Each driver's ``aopen`` refuses (fatal in a non-strict startup too), naming
    ``<capability>.<instance>.url``: one that stayed silent is a late failure the
    development startup only logs, and token revocation then fails open."""
    registry = _registry(capability, name, driver, {})
    with pytest.raises(InfraStartupError) as caught:
        await registry.aopen_all(strict=False)
    assert f"{capability}.{name}.url" in str(caught.value)
    assert "INFRA=" in str(caught.value)
    assert "CACHE_URL" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_default_client")
@pytest.mark.parametrize(("capability", "driver"), _REDIS_DRIVERS)
async def test_the_refusal_of_a_url_less_driver_is_fatal_at_startup(
    capability: str, driver: str
) -> None:
    provider = _registry(capability, "default", driver, {}).provider(
        capability, "default"
    )
    with pytest.raises(MissingUrlError) as caught:
        await provider.aopen()
    assert caught.value.fatal_at_startup is True


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["atomic_store", "broadcast", "lease"])
async def test_a_driver_with_a_url_passes_the_url_check_and_fails_on_the_server(
    capability: str,
) -> None:
    """The url check does not mask what comes after it: an unreachable server is
    still the driver's own (non-fatal) error, not ``MissingUrlError``."""
    provider = _registry(
        capability, "default", "redis", {"url": "redis://127.0.0.1:1/0"}
    ).provider(capability, "default")
    with pytest.raises(Exception) as caught:
        await provider.aopen()
    assert not isinstance(caught.value, MissingUrlError)
    assert getattr(caught.value, "fatal_at_startup", False) is False


@pytest.mark.asyncio
async def test_a_url_less_streams_bus_runs_on_the_default_cache_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback it really uses at runtime: with a default cache client that
    has a url (or a client injected for the process), the bus opens; with none it
    refuses."""
    monkeypatch.setattr(redis_cache, "_default_client", None)
    monkeypatch.setattr(redis_cache, "_default_options", None)
    bus = _registry("eventbus", "default", "streams", {}).provider(
        "eventbus", "default"
    )
    with pytest.raises(MissingUrlError, match=r"default cache client has none"):
        await bus.aopen()
    # a default client with options but no url is no fallback either
    monkeypatch.setattr(
        redis_cache, "_default_options", redis_cache.RedisCacheOptions()
    )
    with pytest.raises(MissingUrlError, match=r"eventbus\.default\.url"):
        await bus.aopen()
    monkeypatch.setattr(
        redis_cache,
        "_default_options",
        redis_cache.RedisCacheOptions(url="redis://127.0.0.1:1/0"),
    )
    await bus.aopen()  # the default cache client has a url: nothing to refuse
    monkeypatch.setattr(redis_cache, "_default_options", None)
    monkeypatch.setattr(redis_cache, "_default_client", object())
    await bus.aopen()  # a client injected for the process needs no url


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_default_client")
async def test_a_url_less_bus_listed_before_the_cache_still_finds_its_url() -> None:
    """INFRA may list ``eventbus`` before ``cache``: the startup opens the cache
    first, so the bus finds the default cache client's url."""
    config: InfraConfig = {
        "eventbus": {"default": InstanceConfig("eventbus", "default", "streams", {})},
        "cache": {
            "default": InstanceConfig(
                "cache", "default", "redis", {"url": "redis://127.0.0.1:1/0"}
            )
        },
    }
    registry = build_infra_registry(
        InMemorySchemaRegistry(), config=config, load_plugins=True
    )
    await registry.aopen_all(strict=True)  # the bus refused here before


_BOOT = """
import asyncio
from sandloom_standard.main import app

async def main():
    async with app.router.lifespan_context(app):
        print("STARTED")

asyncio.run(main())
"""


@pytest.mark.slow
@pytest.mark.subprocess_probe
@pytest.mark.parametrize(
    ("infra", "option"),
    [
        # the standard composition's durable bus has no url and no default
        # cache client to run on (the cache is in memory)
        ({"cache": {"default": {"driver": "memory"}}}, "eventbus.default.url"),
        # the same app with the bus in memory: the security store INFRA selects
        (
            {
                "cache": {"default": {"driver": "memory"}},
                "eventbus": {"default": {"driver": "memory"}},
                "atomic_store": {"security": {"driver": "redis"}},
            },
            "atomic_store.security.url",
        ),
    ],
)
def test_a_development_app_with_a_url_less_redis_instance_refuses_to_start(
    tmp_path: Path, infra: dict[str, object], option: str
) -> None:
    """The review's fail-open: a dev (non-strict) app logged the late failure and
    went on, and the revocation / block checks (``except Exception``) accepted
    every token. A missing url now aborts startup in every environment, naming
    the INFRA option."""
    script = tmp_path / "boot.py"
    script.write_text(_BOOT)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "ENVIRONMENT": "dev",
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        "JWT_SECRET": "url-less-redis-test-secret-0123456789abcdef0123",
        "SANDLOOM_SERVICE_NAME": "url-less",
        "SANDLOOM_COMPOSITION_ROOT": "sandloom_standard.main",
        "INFRA": json.dumps(infra),
    }
    run = subprocess.run(
        [sys.executable, "-I", str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert run.returncode != 0, run.stdout
    assert "STARTED" not in run.stdout
    assert "CriticalStartupError" in run.stderr
    assert option in run.stderr
    assert "CACHE_URL" not in run.stderr


@pytest.mark.asyncio
async def test_a_celery_taskqueue_without_a_broker_refuses_through_aopen_all() -> None:
    registry = _registry("taskqueue", "jobs", "celery", {})
    with pytest.raises(InfraStartupError, match=r"taskqueue\.jobs\.broker"):
        await registry.aopen_all(strict=False)
