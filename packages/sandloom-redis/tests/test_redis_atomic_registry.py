"""The kernel's policy for the ``redis`` atomic_store (``never_evicts_check=assume``),
through the registry, as an application builds the driver, on the test Redis
(``SANDLOOM_TEST_CACHE_URL``).

The driver's own tests (the kit, the eviction check, the round trips) are in
``test_redis_atomic_store.py``; the ``memory`` driver's are the kernel's.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from _redis_required import cache_url_or_unavailable, require_redis

from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.errors import PermissionDenied
from sandloom.core.schema.registry import InMemorySchemaRegistry

_VALUE_LIMIT = 8192


def _registry_store(*, is_prod: bool, risks: tuple[str, ...] = ()) -> Any:
    """The ``redis`` atomic_store the registry builds, ``never_evicts_check=assume``."""
    registry_settings: Any = SimpleNamespace(
        is_prod=is_prod, accepts_risk=lambda risk: risk in risks, infra={}
    )
    config = {
        "atomic_store": {
            "default": InstanceConfig(
                "atomic_store",
                "default",
                "redis",
                {
                    "url": cache_url_or_unavailable(),
                    "max_value_bytes": _VALUE_LIMIT,
                    "never_evicts_check": "assume",
                },
            )
        }
    }
    registry = build_infra_registry(
        InMemorySchemaRegistry(),
        settings=registry_settings,
        config=config,
    )
    return registry.provider("atomic_store", "default")


@pytest.mark.parametrize(
    ("is_prod", "risks"),
    [(False, ()), (True, ("unverified_noeviction",))],
)
async def test_the_registry_opens_assume_in_dev_and_with_the_accepted_risk(
    is_prod: bool, risks: tuple[str, ...]
) -> None:
    await require_redis(cache_url_or_unavailable())
    provider = _registry_store(is_prod=is_prod, risks=risks)
    await provider.aopen()  # a real server: the policy is readable and noeviction
    await provider.aclose()


async def test_the_registry_refuses_assume_in_production_without_the_risk() -> None:
    provider = _registry_store(is_prod=True)
    with pytest.raises(PermissionDenied, match="unverified_noeviction"):
        await provider.aopen()
