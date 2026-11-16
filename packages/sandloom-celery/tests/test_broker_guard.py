"""The real-broker guard: an unreachable broker is an error where the lane says
the broker is required (CI), and a skip with its reason only elsewhere; where it
is required the broker must be a network transport. The required/skip decision
itself is the driverkit guard's (``required_or_skip``); these tests hold what the
package adds on top: the probe, the scheme and the wheel checks."""

from __future__ import annotations

import pytest
from _celery_broker_guard import REQUIRED_ENV, require_broker, source_tree_gap

#: Nothing listens on port 1: refused at once. The password must not be echoed.
_REFUSED = "redis://:s3cret-pass@127.0.0.1:1/0"


@pytest.fixture
def required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REQUIRED_ENV, "1")


@pytest.fixture
def not_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(REQUIRED_ENV, raising=False)


@pytest.mark.usefixtures("required")
def test_a_required_broker_that_does_not_answer_fails_the_run() -> None:
    # a skip must turn this red, not mark the test skipped: catch both outcomes
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception)) as caught:
        require_broker(_REFUSED)

    assert isinstance(caught.value, pytest.fail.Exception), caught.value
    assert REQUIRED_ENV in str(caught.value)
    assert "does not answer" in str(caught.value)
    assert "s3cret" not in str(caught.value)
    assert "127.0.0.1" not in str(caught.value)
    assert (
        caught.value.__context__ is None
    )  # no chained connection error (its text names the host)


@pytest.mark.usefixtures("not_required")
def test_a_broker_that_does_not_answer_skips_where_it_is_not_required() -> None:
    with pytest.raises(pytest.skip.Exception, match="does not answer") as caught:
        require_broker(_REFUSED)

    assert "s3cret" not in str(caught.value)


@pytest.mark.usefixtures("not_required")
def test_a_broker_that_answers_passes_where_it_is_not_required() -> None:
    require_broker("memory://")


@pytest.mark.usefixtures("required")
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
        require_broker(url)

    assert REQUIRED_ENV in str(caught.value)


@pytest.mark.usefixtures("not_required")
def test_a_failure_other_than_an_unreachable_broker_is_not_a_skip() -> None:
    """The guard skips only for a broker that does not answer; a transport kombu
    cannot build is a broken setup and raises."""
    with pytest.raises(Exception) as caught:  # any kombu error type
        require_broker("nosuchtransport://host/0")

    assert not isinstance(caught.value, pytest.skip.Exception)


@pytest.mark.usefixtures("required")
def test_a_required_lane_refuses_the_package_imported_from_the_source_tree() -> None:
    source = "/repo/packages/sandloom-celery/src/sandloom_celery/__init__.py"
    gap = source_tree_gap(source)

    assert gap is not None
    assert "built wheel" in gap
    assert REQUIRED_ENV in gap


@pytest.mark.usefixtures("required")
def test_a_required_lane_accepts_the_installed_wheel() -> None:
    wheel = "/venv/lib/python3.12/site-packages/sandloom_celery/__init__.py"

    assert source_tree_gap(wheel) is None


@pytest.mark.usefixtures("not_required")
def test_the_source_tree_is_fine_where_the_lane_is_not_required() -> None:
    source = "/repo/packages/sandloom-celery/src/sandloom_celery/__init__.py"

    assert source_tree_gap(source) is None
