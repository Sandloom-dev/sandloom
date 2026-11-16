"""The Hypothesis profile of the suite: fewer examples on a PR, all of them in full validation.

Two profiles, picked by ``HYPOTHESIS_PROFILE`` (default ``ci``; ``full-validation``
sets ``full`` through ``ci.yml``):

- ``full``: every property runs the number of examples its test names;
- ``ci``: the same properties with a quarter of the examples (never fewer than
  ``MIN_EXAMPLES``), which is what a pull request and a local run need to find a
  regression; the full count is what finds the rare counterexample.

A test names its full count through :func:`examples`, so no property is dropped,
only its example count shrinks. ``tests/conftest.py`` loads the profile, so the
tests that name no count follow it too. An unknown name is refused, never a
quiet fallback to one of the two.

The loaded profile is process-global (``settings.load_profile``), so it is
session-wide: in a pytest session that also collects ``src/`` (for example
``src/sandloom/core/tests/test_guarantee_lattice.py``), a test with no explicit
count runs 25 examples under ``ci`` instead of Hypothesis's default 100.

A property whose assertion is reached by few generated inputs (the authz laws)
names a fixed ``max_examples`` instead of :func:`examples`, so every profile keeps it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from hypothesis import settings

PROFILES = {"ci": 25, "full": 100}  # profile -> the examples of a test naming none
DEFAULT_PROFILE = "ci"
#: A ``ci`` property keeps at least this many examples.
MIN_EXAMPLES = 20
#: A ``ci`` property runs ``1/CI_DIVISOR`` of its full count.
CI_DIVISOR = 4


def profile_name(environ: Mapping[str, str] | None = None) -> str:
    """The profile ``HYPOTHESIS_PROFILE`` names (``ci`` when unset)."""
    name = (os.environ if environ is None else environ).get(
        "HYPOTHESIS_PROFILE", DEFAULT_PROFILE
    )
    if name not in PROFILES:
        raise ValueError(
            f"HYPOTHESIS_PROFILE={name!r} is not one of {sorted(PROFILES)}"
        )
    return name


def examples(full: int, *, profile: str | None = None) -> int:
    """``full`` examples under ``full``, a quarter of them (at least
    ``MIN_EXAMPLES``, at most ``full``) under ``ci``."""
    if (profile or profile_name()) == "full":
        return full
    return min(full, max(MIN_EXAMPLES, -(-full // CI_DIVISOR)))


def load() -> str:
    """Register both profiles and load the one the environment names."""
    for name, count in PROFILES.items():
        settings.register_profile(name, max_examples=count)
    name = profile_name()
    settings.load_profile(name)
    return name
