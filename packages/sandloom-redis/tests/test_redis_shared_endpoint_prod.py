"""In production a synthesized instance on the cache's database is a WARNING.

A development default (``implicit``: the kernel synthesized it) shares the cache's
Redis by construction, so its startup message is INFO. In production the kernel
synthesizes nothing worth that excuse: the registry hands the provider
``implicit = False`` and the driver words the same overlap as a WARNING. This
runs the real redis drivers end to end, for development and for production on
the same configuration.

Lane: ``redis-drivers`` (the provider rows run on the test Redis
``SANDLOOM_TEST_CACHE_URL``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from urllib.parse import urlsplit, urlunsplit

import pytest
import sandloom_redis.cache as redis_cache
from _redis_required import require_redis

import sandloom.core.infra.registry as registry_module
from sandloom.core.config.settings import Environment, settings
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.infra.config import InfraConfig, InstanceConfig
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.driverkit.shared_endpoints import reset
from sandloom.sdk.testing import resolved_cache_url

_LOGGER = "sandloom.driverkit.shared_endpoints"
_SHARED_DB = 205


@pytest.fixture(autouse=True)
def _clean_rendezvous(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    reset()
    monkeypatch.setattr(redis_cache, "_default_options", None)
    yield
    reset()


def _records(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _LOGGER and r.levelno == level
    ]


def _config(url: str) -> InfraConfig:
    """``cache.default`` and two synthesized instances, all on one database."""
    return {
        "cache": {"default": InstanceConfig("cache", "default", "redis", {"url": url})},
        "atomic_store": {
            "dev": InstanceConfig(
                "atomic_store",
                "dev",
                "redis",
                {"url": url, "never_evicts_check": "assume"},
                implicit=True,
            ),
        },
        "lease": {
            "beat": InstanceConfig(
                "lease", "beat", "redis", {"url": url}, implicit=True
            )
        },
    }


async def _run(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    environment: Environment,
) -> None:
    base = resolved_cache_url()
    await require_redis(base)
    url = urlunsplit(urlsplit(base)._replace(path=f"/{_SHARED_DB}"))
    monkeypatch.setattr(
        registry_module,
        "settings",
        settings.model_copy(update={"environment": environment}),
    )
    registry = build_infra_registry(
        InMemorySchemaRegistry(), settings=settings, config=_config(url)
    )
    try:
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            await registry.provider("atomic_store", "dev").aopen()
            await registry.provider("lease", "beat").aopen()
            registry.provider("cache", "default")
    finally:
        await registry.aclose_all()


async def test_development_logs_the_synthesized_overlap_at_info(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run(caplog, monkeypatch, Environment.dev)
    assert _records(caplog, logging.WARNING) == []
    infos = _records(caplog, logging.INFO)
    assert len(infos) == 2
    assert any(m.startswith("atomic_store instance 'dev'") for m in infos)
    assert any(m.startswith("lease instance 'beat'") for m in infos)


async def test_production_warns_for_the_same_synthesized_overlap(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run(caplog, monkeypatch, Environment.prod)
    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == 2
    assert any(m.startswith("atomic_store instance 'dev'") for m in warnings)
    assert any(m.startswith("lease instance 'beat'") for m in warnings)
    assert _records(caplog, logging.INFO) == []
