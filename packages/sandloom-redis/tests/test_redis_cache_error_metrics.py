"""The Redis cache counts backend errors, never a caller's own."""

from __future__ import annotations

from typing import Any

import pytest
from prometheus_client import REGISTRY
from redis.exceptions import ConnectionError as RedisConnectionError
from sandloom_redis.cache import RedisCacheProvider

from sandloom.core.infra.errors import Unavailable


def _value(name: str, labels: dict[str, str]) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


async def test_redis_cache_counts_backend_errors_not_caller_errors() -> None:
    class _Client:
        async def get(self, key: str) -> str:
            raise RedisConnectionError("down")

        async def set(self, key: str, value: str) -> None:
            raise ValueError("a caller's bug")

    provider = RedisCacheProvider(
        name="errtest", driver="redis", options={}, shared=False
    )

    async def _client_obj() -> Any:
        return _Client()

    provider._client_obj = _client_obj  # type: ignore[method-assign]
    labels = {"instance": "errtest", "operation": "get"}
    before = _value("sandloom_cache_operation_errors_total", labels)
    with pytest.raises(Unavailable):
        await provider.get("k")
    assert _value("sandloom_cache_operation_errors_total", labels) == before + 1
    with pytest.raises(ValueError):
        await provider.set("k", "v")
    assert (
        _value(
            "sandloom_cache_operation_errors_total",
            {"instance": "errtest", "operation": "set"},
        )
        == 0
    )
