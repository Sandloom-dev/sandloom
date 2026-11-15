"""The real-broker guard: an unreachable broker is an error where the lane says
the broker is required (CI), and a skip with its reason only elsewhere; where it
is required the broker must be a network transport."""

from __future__ import annotations

import pytest
from _celery_broker_guard import (
    REQUIRED_ENV,
    broker_required,
    require_broker,
    source_tree_gap,
)

#: Nothing listens on port 1: refused at once. The password must not be echoed.
_REFUSED = "redis://:s3cret-pass@127.0.0.1:1/0"


def test_a_required_broker_that_does_not_answer_fails_the_run() -> None:
    with pytest.raises(pytest.fail.Exception, match=REQUIRED_ENV) as caught:
        require_broker(_REFUSED, required=True)

    assert "s3cret" not in str(caught.value)
    assert "127.0.0.1" not in str(caught.value)


def test_a_broker_that_does_not_answer_skips_where_it_is_not_required() -> None:
    with pytest.raises(pytest.skip.Exception, match="does not answer") as caught:
        require_broker(_REFUSED, required=False)

    assert "s3cret" not in str(caught.value)


def test_a_broker_that_answers_passes_where_it_is_not_required() -> None:
    require_broker("memory://", required=False)


@pytest.mark.parametrize(
    "url", ["memory://", "filesystem://", "sqla+sqlite:///x.db", "cache+memory://"]
)
def test_a_required_broker_that_is_not_a_network_transport_fails_the_run(
    url: str,
) -> None:
    """Such a broker answers always: it would turn the lane green without a server."""
    with pytest.raises(
        pytest.fail.Exception, match="not a network transport"
    ) as caught:
        require_broker(url, required=True)

    assert REQUIRED_ENV in str(caught.value)


def test_a_failure_other_than_an_unreachable_broker_is_not_a_skip() -> None:
    """The guard skips only for a broker that does not answer; a transport kombu
    cannot build is a broken setup and raises."""
    with pytest.raises(Exception) as caught:  # any kombu error type
        require_broker("nosuchtransport://host/0", required=False)

    assert not isinstance(caught.value, pytest.skip.Exception)


def test_a_required_lane_refuses_the_package_imported_from_the_source_tree() -> None:
    source = "/repo/packages/sandloom-celery/src/sandloom_celery/__init__.py"
    gap = source_tree_gap(source, required=True)

    assert gap is not None
    assert "built wheel" in gap
    assert REQUIRED_ENV in gap


def test_a_required_lane_accepts_the_installed_wheel() -> None:
    wheel = "/venv/lib/python3.12/site-packages/sandloom_celery/__init__.py"

    assert source_tree_gap(wheel, required=True) is None


def test_the_source_tree_is_fine_where_the_lane_is_not_required() -> None:
    source = "/repo/packages/sandloom-celery/src/sandloom_celery/__init__.py"

    assert source_tree_gap(source, required=False) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("0", False), ("", False), ("true", False), (None, False)],
)
def test_only_one_makes_the_broker_required(value: str | None, expected: bool) -> None:
    environ = {} if value is None else {REQUIRED_ENV: value}

    assert broker_required(environ) is expected
