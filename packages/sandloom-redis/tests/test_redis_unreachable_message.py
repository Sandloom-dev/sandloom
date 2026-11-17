"""The kernel's one-line "cannot reach ..." message, on the real client's errors.

The kernel names no Redis class: a driver maps its client's errors onto
``InfraError`` (``sandloom_redis._errors``) and the line names the capability
that failed (``sandloom.core.observability.unreachable``). This proves that what
the package raises reads as one line, and that a raw client error nobody mapped
does not.
"""

from __future__ import annotations

from redis.exceptions import AuthenticationError, ConnectionError, RedisError
from sandloom_redis._errors import map_redis_error

from sandloom.core.observability.unreachable import connection_failure


def _mapped(raw: BaseException, capability: str = "cache") -> BaseException:
    mapped = map_redis_error(raw, capability=capability, instance=None, what="get")
    mapped.__cause__ = raw
    return mapped


def test_a_refused_redis_connection_becomes_one_line() -> None:
    raw = ConnectionError("Error 61 connecting to 127.0.0.1:6379. Connection refused.")
    line = connection_failure(_mapped(raw))
    assert line is not None
    assert (
        "cannot reach the cache backend at 127.0.0.1:6379 (connection refused)" in line
    )
    assert "Redis" not in line


def test_the_line_names_the_capability_of_the_failing_driver() -> None:
    raw = ConnectionError("Error 61 connecting to 127.0.0.1:6379. Connection refused.")
    for capability, label in (
        ("atomic_store", "the atomic store backend"),
        ("eventbus", "the event bus"),
        ("taskqueue", "the task queue"),
    ):
        line = connection_failure(_mapped(raw, capability))
        assert line is not None
        assert f"cannot reach {label} at 127.0.0.1:6379" in line


def test_a_redis_login_refusal_names_the_login_not_the_address() -> None:
    line = connection_failure(_mapped(AuthenticationError("bad credentials")))
    assert line is not None
    assert "the cache backend refused the login (authentication failed)" in line
    assert "cannot reach" not in line


def test_a_redis_error_that_says_nothing_about_the_connection_is_no_one_line() -> None:
    assert connection_failure(_mapped(RedisError("wrong number of arguments"))) is None


def test_a_raw_client_error_nobody_mapped_is_no_one_line() -> None:
    """The kernel matches no client module: only a driver's mapping is read."""
    raw = ConnectionError("Error 61 connecting to 127.0.0.1:6379. Connection refused.")
    assert connection_failure(raw) is None
