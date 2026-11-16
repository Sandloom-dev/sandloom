"""The kernel's one-line "cannot reach Redis" message, on the real client's errors.

The kernel recognises a Redis client's error by its module and class name
(``sandloom.core.observability.unreachable``) and imports no client; this
proves its stand-in in ``test_dev_command.py`` matches what ``redis`` raises.
"""

from __future__ import annotations

from redis.exceptions import AuthenticationError, ConnectionError, RedisError

from sandloom.core.observability.unreachable import connection_failure


def test_a_refused_redis_connection_becomes_one_line() -> None:
    exc = ConnectionError("Error 61 connecting to 127.0.0.1:6379. Connection refused.")
    line = connection_failure(exc)
    assert line is not None
    assert "cannot reach Redis at 127.0.0.1:6379 (connection refused)" in line


def test_a_redis_login_refusal_names_the_login_not_the_address() -> None:
    line = connection_failure(AuthenticationError("bad credentials"))
    assert line is not None
    assert "Redis refused the login (authentication failed)" in line
    assert "cannot reach" not in line


def test_a_redis_error_that_says_nothing_about_the_connection_is_no_one_line() -> None:
    assert connection_failure(RedisError("wrong number of arguments")) is None
