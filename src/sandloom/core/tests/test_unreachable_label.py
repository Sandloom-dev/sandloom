"""The SANDLOOM-R014 line names a capability, never a product it was not told.

The kernel matches no client library: a driver maps its client's errors onto
``InfraError`` and the line says which capability failed. A broker wrapper is
not labelled "Redis" either (the task queue's broker can be anything).
"""

from __future__ import annotations

import pytest

from sandloom.core.exceptions import DatabaseUnavailable
from sandloom.core.infra.errors import (
    InvalidArgument,
    PermissionDenied,
    Timeout,
    Unavailable,
)
from sandloom.core.infra.ports.database import BrokerUnavailableError
from sandloom.core.observability import unreachable as unreachable_module
from sandloom.core.observability.unreachable import (
    connection_failure,
    unreachable,
)

_REFUSED = "Error 61 connecting to 127.0.0.1:6379. Connection refused."


def _client_error(name: str, module: str, text: str) -> Exception:
    """A client library's own error class, named the way the real one is."""
    return type(name, (Exception,), {"__module__": module})(text)


def _chained(error: BaseException, cause: BaseException) -> BaseException:
    error.__cause__ = cause
    return error


def test_a_mapped_error_is_labelled_by_its_capability() -> None:
    error = _chained(
        Unavailable("cache 'default' get: ConnectionError", capability="cache"),
        _client_error("ConnectionError", "somelib.exceptions", _REFUSED),
    )
    line = connection_failure(error)
    assert line is not None
    assert (
        "cannot reach the cache backend at 127.0.0.1:6379 (connection refused)" in line
    )


def test_a_named_instance_is_part_of_the_label() -> None:
    error = _chained(
        Unavailable("x", capability="atomic_store", instance="security"),
        _client_error("ConnectionError", "somelib", _REFUSED),
    )
    line = connection_failure(error)
    assert line is not None
    assert "the atomic store backend 'security' at 127.0.0.1:6379" in line


def test_a_timeout_and_a_refused_login_are_labelled_too() -> None:
    slow = _chained(
        Timeout("x", capability="eventbus"),
        _client_error(
            "TimeoutError", "somelib", "Timeout connecting to 127.0.0.1:6379"
        ),
    )
    line = connection_failure(slow)
    assert line is not None
    assert "cannot reach the eventbus backend" in line
    refused = _chained(
        PermissionDenied("x", capability="cache"),
        _client_error("AuthenticationError", "somelib", "invalid password"),
    )
    line = connection_failure(refused)
    assert line is not None
    assert "the cache backend refused the login" in line


def test_the_broker_wrapper_is_not_labelled_redis() -> None:
    error = _chained(
        BrokerUnavailableError("the broker is down"),
        _client_error("ConnectionError", "kombu.exceptions", _REFUSED),
    )
    line = connection_failure(error)
    assert line is not None
    assert "Redis" not in line
    assert "cannot reach the event bus at 127.0.0.1:6379" in line


def test_the_capability_of_a_mapped_cause_labels_the_broker_wrapper_chain() -> None:
    """With no wrapper at the top, the chain's mapped error still labels it."""
    inner = _chained(
        Unavailable("x", capability="taskqueue"),
        _client_error("ConnectionError", "somelib", _REFUSED),
    )
    wrapper = _chained(RuntimeError("startup task failed"), inner)
    line = unreachable(wrapper)
    assert line is not None
    assert line.startswith("cannot reach the taskqueue backend at 127.0.0.1:6379")


def test_a_database_failure_still_reads_postgresql() -> None:
    error = _chained(
        DatabaseUnavailable("down"),
        _client_error("ConnectionError", "somelib", "connection refused"),
    )
    line = connection_failure(error)
    assert line is not None
    assert "cannot reach PostgreSQL" in line


@pytest.mark.parametrize(
    ("name", "module"),
    [
        ("ConnectionError", "redis.exceptions"),
        ("ConnectionError", "kombu.exceptions"),
        ("OperationalError", "kombu.exceptions"),
    ],
)
def test_a_raw_client_error_is_recognised_by_no_module_name(
    name: str, module: str
) -> None:
    assert connection_failure(_client_error(name, module, _REFUSED)) is None


def test_a_mapped_error_that_names_no_connection_fault_is_no_one_line() -> None:
    assert (
        connection_failure(
            _chained(
                InvalidArgument("x", capability="cache"),
                _client_error("ConnectionError", "somelib", _REFUSED),
            )
        )
        is None
    )
    assert connection_failure(Unavailable("x", capability="cache")) is None


def test_the_recognised_driver_packages_name_no_redis_or_kombu() -> None:
    assert not {"redis", "kombu"} & set(unreachable_module._DRIVERS)
