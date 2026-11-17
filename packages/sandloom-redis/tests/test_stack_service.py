"""The Redis server of the standard stack (``sandloom_redis.stack``): what
``sandloom dev`` and the scaffold of ``sandloom new`` ask of it, and that the
kernel finds it through the ``sandloom.stack`` entry point.

Needs no server: the checks on a live one are in ``test_dev_external_redis.py``.
"""

from __future__ import annotations

import re

import pytest
import redis
from sandloom_redis.stack import STACK_SERVICE, RedisServer

from sandloom.core.cli import stack_service as kit

_SECRET = "S3CRETpw"


def test_the_kernel_finds_the_service_through_the_entry_point() -> None:
    assert kit.load() is STACK_SERVICE
    assert kit.STANDARD_DRIVER == "redis"


def test_a_driver_nobody_describes_is_refused_with_the_install_hint() -> None:
    with pytest.raises(kit.StackServiceError, match=r"sandloom\[redis\]") as refused:
        kit.load("no-such-driver")
    assert "no-such-driver" in str(refused.value)


@pytest.mark.parametrize(
    ("url", "says"),
    [
        (f"rediss://:{_SECRET}@cache.local:6380", "rediss:// (TLS) is not supported"),
        (f"redis://app:{_SECRET}@cache.local:6379", "ACL user"),
        ("redis://cache.local:6379?ssl_cert_reqs=none", "URL options"),
        (f"postgresql://u:{_SECRET}@h:5432/db", "not a Redis URL"),
    ],
)
def test_a_url_whose_tls_or_user_would_be_dropped_is_refused(
    url: str, says: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(says)) as refused:
        STACK_SERVICE.parse(url)
    assert _SECRET not in str(refused.value)


def test_a_url_urlsplit_refuses_never_puts_its_password_in_the_error() -> None:
    hostile = f"redis://user:{_SECRET}@host{chr(0xFF03)}x:6379/0"
    with pytest.raises(ValueError, match="cannot be parsed") as caught:
        RedisServer.from_url(hostile)
    assert _SECRET not in str(caught.value)


def test_a_url_round_trips_with_its_password_and_default_port() -> None:
    server = STACK_SERVICE.parse("redis://:p%40ss@cache.local")
    assert (server.host, server.port, server.password) == ("cache.local", 6379, "p@ss")
    assert server.url(3) == "redis://:p%40ss@cache.local:6379/3"
    assert server.url() == "redis://:p%40ss@cache.local:6379"
    assert server.address == "cache.local:6379"
    assert STACK_SERVICE.local_url("127.0.0.1") == "redis://127.0.0.1:6379"
    assert STACK_SERVICE.server("h", 1).url(0) == "redis://h:1/0"


def test_the_clients_errors_are_the_ones_the_engine_catches() -> None:
    assert redis.RedisError in STACK_SERVICE.errors()
    assert issubclass(redis.ConnectionError, STACK_SERVICE.errors())


def test_the_dev_compose_block_is_the_server_of_the_named_service() -> None:
    dev = STACK_SERVICE.dev_compose("run `sandloom dev up`", test=False)
    assert dev.startswith(f"  {STACK_SERVICE.name}:\n")
    assert "image: redis:7-alpine" in dev
    assert f"${{{STACK_SERVICE.port_var}:?run `sandloom dev up`}}:6379" in dev
    test = STACK_SERVICE.dev_compose("run `sandloom dev up`", test=True)
    assert test.startswith(f"  {STACK_SERVICE.test_name}:\n")
    assert STACK_SERVICE.test_port_var in test
    assert STACK_SERVICE.port_var not in test
