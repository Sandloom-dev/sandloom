"""The required-backend guard of the driver packages' test lanes.

``required_or_skip`` fails where the lane variable is exactly ``1`` and skips
elsewhere; the message names the variable and the reason. ``lane_required`` is
the same predicate for a probe that needs more than the skip (a scheme check).
"""

from __future__ import annotations

import pytest

from sandloom.driverkit import testing
from sandloom.driverkit.testing import lane_required, required_or_skip

VAR = "SANDLOOM_EXAMPLE_REQUIRED"


def _outcome(env_var: str, reason: str = "no backend") -> BaseException:
    """What ``required_or_skip`` ends in. A skip raised under ``pytest.raises``
    for another outcome would mark the test skipped, not failed: catch every
    outcome here, so a guard that skips where it must fail or refuse turns the
    test red."""
    try:
        required_or_skip(env_var, reason)
    except (ValueError, pytest.fail.Exception, pytest.skip.Exception) as outcome:
        return outcome
    raise AssertionError("required_or_skip returned")


def test_the_guard_is_reachable_from_the_one_public_path() -> None:
    assert {"lane_required", "required_or_skip"} <= set(testing.__all__)
    assert testing.required_or_skip is required_or_skip


def test_a_missing_backend_skips_where_the_lane_is_not_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(VAR, raising=False)
    outcome = _outcome(VAR)
    assert isinstance(outcome, pytest.skip.Exception), outcome
    assert str(outcome) == f"no backend ({VAR}=1 would make this an error)"


@pytest.mark.parametrize("value", ["0", "", "true", "yes", "11", " 1"])
def test_only_exactly_one_makes_the_lane_required(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(VAR, value)
    assert isinstance(_outcome(VAR), pytest.skip.Exception)
    assert lane_required(VAR) is False


def test_a_missing_backend_fails_where_the_lane_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(VAR, "1")
    outcome = _outcome(VAR)
    assert isinstance(outcome, pytest.fail.Exception), outcome
    assert str(outcome) == f"no backend ({VAR}=1: this lane may not skip)"
    assert lane_required(VAR) is True


def test_the_guard_reads_the_variable_it_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another package's lane variable does not make this lane required."""
    monkeypatch.setenv("SANDLOOM_OTHER_REQUIRED", "1")
    monkeypatch.delenv(VAR, raising=False)
    assert isinstance(_outcome(VAR), pytest.skip.Exception)


def test_lane_required_reads_a_mapping_when_given_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(VAR, "1")
    assert lane_required(VAR, {}) is False
    assert lane_required(VAR, {VAR: "1"}) is True
    assert lane_required(VAR, {VAR: "0"}) is False


@pytest.mark.parametrize("name", ["", "lower_case", "WITH SPACE", "1LEADING", "A-B"])
def test_a_malformed_variable_name_is_refused(name: str) -> None:
    outcome = _outcome(name)
    assert isinstance(outcome, ValueError), outcome
    assert "variable name" in str(outcome)
    with pytest.raises(ValueError, match="variable name"):
        lane_required(name)


@pytest.mark.parametrize("reason", ["", "   "])
def test_an_empty_reason_is_refused(reason: str) -> None:
    outcome = _outcome(VAR, reason)
    assert isinstance(outcome, ValueError), outcome
    assert "reason" in str(outcome)
