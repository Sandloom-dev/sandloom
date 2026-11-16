"""``sandloom.driverkit.shared_endpoints``: the rendezvous of the startup warning
for a persistent instance on the Redis database of ``cache.default``.

No driver package imports another, so the cache's driver and the driver of a
persistent instance meet here, whichever starts first. Lane: the no-Redis lane
over ``src/sandloom/core/tests`` (pure logic, no connection).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from sandloom.core.config.settings import settings
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.infra.config import InfraConfig, InstanceConfig
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.driverkit.shared_endpoints import (
    redis_endpoint,
    register,
    reset,
    unregister,
)

_LOGGER = "sandloom.driverkit.shared_endpoints"
_SECRET = "s3cret-pw"
_CACHE = "cache-host:6379/2"


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    reset()
    yield
    reset()


def _records(
    caplog: pytest.LogCaptureFixture, level: int = logging.WARNING
) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _LOGGER and r.levelno == level
    ]


def _persistent(
    instance: str = "security",
    endpoint: str | None = _CACHE,
    *,
    capability: str = "atomic_store",
    declared: bool = True,
) -> None:
    register(
        capability,
        instance,
        endpoint,
        "persistent",
        option=f"{capability}.{instance}.url",
        declared=declared,
    )


def _cache(endpoint: str | None = _CACHE) -> None:
    register("cache", "default", endpoint, "cache_default")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("redis://Host:6380/3", "host:6380/3"),
        ("redis://host", "host:6379/0"),
        ("redis://host:6379/", "host:6379/0"),
        ("redis://host/?db=4", "host:6379/4"),
        ("redis://:pw@host:6379/1", "host:6379/1"),
        ("rediss://user:pw@host:6379/1?ssl_cert_reqs=none", "host:6379/1"),
        ("memory://", None),
        ("amqp://host:5672//", None),
        ("redis:///0", None),
        ("redis://host:notaport/0", None),
        # an unencoded '/' in the password splits the url inside it: nothing of
        # the credential may reach an endpoint (and so a log line)
        ("redis://admin:1234/5@host", None),
        ("redis://admin:1234/5@host:6379/0", None),
        ("", None),
        (None, None),
    ],
)
def test_the_endpoint_is_host_port_db_without_credentials(
    url: str | None, expected: str | None
) -> None:
    assert redis_endpoint(url) == expected


def test_the_same_database_warns_once_naming_both_and_the_option(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        _cache(redis_endpoint(f"redis://:{_SECRET}@Cache-Host:6379/2"))
        _persistent(endpoint=redis_endpoint(f"redis://:x-{_SECRET}@cache-host/2"))
    [message] = _records(caplog)
    assert "atomic_store instance 'security'" in message
    assert "cache.default" in message
    assert _CACHE in message
    assert "atomic_store.security.url" in message
    assert _SECRET not in caplog.text


@pytest.mark.parametrize(
    ("cache", "own"),
    [
        ("cache-host:6379/2", "cache-host:6379/3"),  # other db
        ("cache-host:6379/2", "cache-host:6380/2"),  # other port
        ("cache-host:6379/2", "other-host:6379/2"),  # other host
        (None, "cache-host:6379/2"),  # the default cache is not a Redis one
        ("cache-host:6379/2", None),  # the instance is not on a Redis
    ],
)
def test_another_endpoint_does_not_warn(
    caplog: pytest.LogCaptureFixture, cache: str | None, own: str | None
) -> None:
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        _cache(cache)
        _persistent(endpoint=own)
    assert _records(caplog) == []
    assert _records(caplog, logging.INFO) == []


@pytest.mark.parametrize("cache_first", [True, False])
def test_the_warning_does_not_depend_on_which_instance_comes_first(
    caplog: pytest.LogCaptureFixture, cache_first: bool
) -> None:
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        if cache_first:
            _cache()
        _persistent()
        if not cache_first:
            assert _records(caplog) == []
            _cache()
    assert len(_records(caplog)) == 1


def test_opening_again_without_closing_does_not_warn_twice(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        _persistent()
        _cache()
        _cache()
        _persistent()
        _persistent()
    assert len(_records(caplog)) == 1


def test_one_cache_warns_for_each_persistent_instance_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        _persistent("a")
        _persistent("b", capability="lease")
        _persistent("c", endpoint="elsewhere:6379/0")
        _cache()
    assert sorted(m.split(" runs")[0] for m in _records(caplog)) == [
        "atomic_store instance 'a'",
        "lease instance 'b'",
    ]


def test_a_closed_app_is_forgotten_and_the_next_one_warns_for_itself(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A second app in one process (the kernel's test suite) must not hear of
    the first app's instances, and its own pair is logged again."""
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        _cache()
        _persistent("first")
        assert len(_records(caplog)) == 1
        unregister("atomic_store", "first", "persistent")
        unregister("cache", "default", "cache_default")
        _persistent("second", endpoint="other-host:6379/1")
        _cache("other-host:6379/9")
        assert len(_records(caplog)) == 1  # nothing for the first, nothing new
        _persistent("first")
        _cache()
        assert len(_records(caplog)) == 2  # the first pair again, in a new app


def test_a_closed_instance_is_not_warned_about_by_a_cache_that_comes_later(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        _persistent()
        unregister("atomic_store", "security", "persistent")
        _cache()
    assert _records(caplog) == []


def test_an_endpoint_of_none_unregisters(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        _persistent()
        _persistent(endpoint=None)  # the url was changed to a non-Redis one
        _cache()
    assert _records(caplog) == []


def test_an_instance_nobody_declared_is_info_never_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The development default (``CACHE_URL`` alone) shares by construction: a
    WARNING on every start would hide the one an operator has to act on."""
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        _cache()
        _persistent(declared=False)
        _persistent("named", declared=True)
    assert len(_records(caplog)) == 1
    assert "'named'" in _records(caplog)[0]
    [implicit] = _records(caplog, logging.INFO)
    assert "'security'" in implicit


def test_reset_forgets_registrations_and_logged_pairs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        _cache()
        _persistent()
        reset()
        _persistent()  # the cache is forgotten: nothing to meet
        assert len(_records(caplog)) == 1
        _cache()
    assert len(_records(caplog)) == 2  # the pair was forgotten with the rest


def test_the_registry_tells_a_provider_whether_the_kernel_synthesized_it() -> None:
    """``InfraProvider.implicit`` is what a driver words its startup message by:
    set from ``InstanceConfig.implicit`` after the build, false otherwise."""
    config: InfraConfig = {
        "lease": {
            "default": InstanceConfig("lease", "default", "memory", {}, implicit=True),
            "named": InstanceConfig("lease", "named", "memory", {}),
        }
    }
    registry = build_infra_registry(
        InMemorySchemaRegistry(), settings=settings, config=config
    )
    assert registry.provider("lease", "default").implicit is True
    assert registry.provider("lease", "named").implicit is False


def test_an_unknown_role_is_refused() -> None:
    with pytest.raises(ValueError, match="role must be one of"):
        register("cache", "default", _CACHE, "other")  # type: ignore[arg-type]
