"""The Redis cache driver declares its metrics through the driverkit hook."""

from __future__ import annotations

from prometheus_client import REGISTRY

from sandloom.core.observability import driver_metrics
from sandloom.core.tests._driver_metrics_support import (
    assert_kernel_metrics_register_before,
)


def test_the_kernel_metrics_register_before_the_redis_cache_driver() -> None:
    assert_kernel_metrics_register_before("sandloom_redis.cache")


def test_the_redis_cache_driver_records_through_the_hook() -> None:
    import sandloom_redis.cache as redis_cache

    assert isinstance(
        redis_cache.cache_operation_errors_total, driver_metrics.DriverCounter
    )
    assert "sandloom_cache_operation_errors" in {m.name for m in REGISTRY.collect()}
