"""The harness isolates a default event bus that names a url but no driver.

The kernel has no default driver and never ``memory``: with
``EVENT_BUS_BACKEND`` unset the composition's ``eventbus.default`` decides, the
standard one's being ``streams`` in every environment (not only ``prod``). An
``INFRA`` entry that gives the default bus options only is therefore a Redis
instance whose url the harness moves per worker, whatever ``ENVIRONMENT`` is.
"""

from __future__ import annotations

import json

import pytest

from sandloom.core.testing import workers

_INFRA = json.dumps({"eventbus": {"default": {"url": "redis://h:6380/5"}}})
_ENV = {workers.WORKER_ENV: "gw1"}


@pytest.mark.parametrize("environment", ["dev", "test", "prod"])
def test_an_options_only_default_bus_is_a_redis_instance_in_every_environment(
    environment: str,
) -> None:
    values = {
        "CACHE_URL": "redis://h:6380/0",
        "ENVIRONMENT": environment,
        "INFRA": _INFRA,
    }
    out = workers.rewrite_for_worker(values, _ENV)
    moved = json.loads(out["INFRA"])["eventbus"]["default"]["url"]
    assert moved != "redis://h:6380/5", out


def test_an_explicit_memory_bus_is_not_moved() -> None:
    values = {
        "CACHE_URL": "redis://h:6380/0",
        "EVENT_BUS_BACKEND": "memory",
        "INFRA": _INFRA,
    }
    out = workers.rewrite_for_worker(values, _ENV)
    moved = json.loads(out["INFRA"])["eventbus"]["default"]["url"]
    assert moved == "redis://h:6380/5", out
