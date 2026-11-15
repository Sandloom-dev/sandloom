"""``RedisCacheProvider.from_config`` builds ``cls``: a subclass gets itself back."""

from __future__ import annotations

from sandloom_redis.cache import RedisCacheProvider, create_redis_cache

from sandloom.core.infra.config import InstanceConfig


class _Cache(RedisCacheProvider):
    pass


def test_a_cache_subclass_builds_itself_and_the_factory_the_base_class() -> None:
    # A named instance builds no client until first use: no Redis is reached.
    config = InstanceConfig(
        capability="cache",
        name="aux",
        driver="redis",
        options={"url": "redis://127.0.0.1:1/0"},
    )
    built = _Cache.from_config(config)
    assert type(built) is _Cache
    assert (built.name, built.driver) == ("aux", "redis")
    assert type(RedisCacheProvider.from_config(config)) is RedisCacheProvider
    assert type(create_redis_cache(config)) is RedisCacheProvider
