"""A failing Redis cache's health names the error type, never the message."""

from __future__ import annotations

import pytest
from sandloom_redis.cache import create_redis_cache

from sandloom.core.infra.config import InstanceConfig


@pytest.mark.asyncio
async def test_the_redis_health_names_the_error_type() -> None:
    provider = create_redis_cache(
        InstanceConfig(
            "cache",
            "probe",
            "redis",
            {"url": "redis://user:s3cret-marker@127.0.0.1:1/0"},
        )
    )
    try:
        result = await provider.health()
    finally:
        await provider.aclose()
    assert result.status == "fail"
    assert result.details is not None
    assert str(result.details["error"]).isidentifier()
    assert "s3cret-marker" not in repr(result)
