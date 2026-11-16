"""The ``redis`` atomic store in the kernel's security duties: the development
default (the security store on the cache's Redis, which exists only where
``sandloom-redis`` is installed) and the strict production profile's
attestation of the real Redis classes' identity.

The attestation logic runs in the kernel on scripted providers
(``src/sandloom/core/tests/test_atomic_adopt.py``); this file runs it on the
``redis`` cache and atomic store against the test Redis.
"""

from __future__ import annotations

from typing import Any

import pytest
from _redis_required import cache_url_or_unavailable, require_redis
from sandloom_redis.atomic_store import RedisAtomicStoreProvider
from sandloom_redis.cache import RedisCacheProvider

from sandloom.core import production_profile
from sandloom.core.config.settings import Settings
from sandloom.core.infra.config import load_infra_config
from sandloom.core.tests._driver_fill import fill
from sandloom.core.tests.test_atomic_adopt import (
    _Provider,
    _runtime_core,
    no_rls_problems,  # noqa: F401 - the fixture of the kernel's test of the same duty
)
from sandloom.core.tests.test_production_profile import _settings


def test_development_builds_the_security_store_on_the_cache_redis_only_outside_prod() -> (
    None
):
    """With ``sandloom-redis`` installed the security store defaults to the
    cache's Redis (without it the kernel selects ``memory``)."""
    dev = Settings(  # type: ignore[call-arg]
        _env_file=None, ENVIRONMENT="dev", CACHE_URL="redis://cache-redis:6379/4"
    )
    config = fill(load_infra_config(dev), dev)
    security = config["atomic_store"]["security"]
    assert security.driver == "redis" and security.implicit
    assert security.options["url"] == "redis://cache-redis:6379/4"
    assert "idempotency" not in config["atomic_store"]


async def test_strict_profile_attests_real_redis_identity(
    no_rls_problems: None,  # noqa: F811
) -> None:
    """The real classes on the test Redis: the cache and the Redis atomic store
    name their server alike (``redis:<run_id>``), so a store on the cache's
    server is refused, and a store whose server differs is attested (spec 12)."""
    url = cache_url_or_unavailable()
    await require_redis(url)
    store = RedisAtomicStoreProvider(
        name="security", driver="redis", options={"url": url}
    )
    cache = RedisCacheProvider(
        name="default", driver="redis", options={"url": url}, shared=False
    )
    try:
        identity = await store.isolation_identity()
        assert identity is not None and identity.startswith("redis:")
        assert await cache.isolation_identity() == identity

        providers: dict[tuple[str, str], Any] = {
            ("cache", "default"): cache,
            ("atomic_store", "security"): store,
        }
        core = _runtime_core(_settings(), providers, {"security"})
        with pytest.raises(
            production_profile.ProductionProfileError,
            match="share one failure domain",
        ):
            await production_profile.enforce_runtime(core)

        providers[("cache", "default")] = _Provider("redis:another-server")
        await production_profile.enforce_runtime(core)
    finally:
        await store.aclose()
        await cache.aclose()
