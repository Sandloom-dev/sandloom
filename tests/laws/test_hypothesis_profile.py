"""The Hypothesis profile (``tests/_hypothesis_profile.py``): ``ci`` shrinks the
example counts, ``full`` keeps them, an unknown profile is refused."""

from __future__ import annotations

import pytest
import test_authz_algebra_laws as authz
from _hypothesis_profile import (
    CI_DIVISOR,
    MIN_EXAMPLES,
    PROFILES,
    examples,
    load,
    profile_name,
)
from hypothesis import settings


@pytest.mark.parametrize("full", [20, 80, 150, 250, 300, 400])
def test_full_keeps_every_example_and_ci_runs_a_part_of_them(full: int) -> None:
    assert examples(full, profile="full") == full
    ci = examples(full, profile="ci")
    assert ci == max(MIN_EXAMPLES, -(-full // CI_DIVISOR))
    assert MIN_EXAMPLES <= ci <= full
    if full >= 4 * MIN_EXAMPLES:
        assert ci < full  # a real saving, not a rename


def test_ci_never_runs_more_than_the_full_count() -> None:
    assert examples(5, profile="ci") == 5


def test_the_profile_follows_the_environment_and_defaults_to_ci() -> None:
    assert profile_name({}) == "ci"
    assert profile_name({"HYPOTHESIS_PROFILE": "full"}) == "full"
    assert examples(400, profile=profile_name({"HYPOTHESIS_PROFILE": "full"})) == 400


@pytest.mark.parametrize("name", ["", "Full", "fast", "default"])
def test_an_unknown_profile_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="HYPOTHESIS_PROFILE"):
        profile_name({"HYPOTHESIS_PROFILE": name})


def test_the_loaded_profile_gives_a_test_naming_no_count_its_examples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = settings.get_current_profile_name()
    try:
        for name, count in PROFILES.items():
            monkeypatch.setenv("HYPOTHESIS_PROFILE", name)
            assert load() == name
            assert settings.default.max_examples == count
    finally:
        settings.load_profile(previous)
    assert PROFILES["ci"] < PROFILES["full"]


def test_the_authz_laws_keep_400_examples_in_every_profile() -> None:
    """Their fail-closed laws reach the assertion on few trees: not a quarter."""
    assert authz.LAWS.max_examples == 400
