"""A test Redis that is missing: a skip locally, a failure in the ``redis-drivers`` lane.

The ``redis-drivers`` CI job sets ``SANDLOOM_REDIS_REQUIRED=1`` because it is the
only job that runs these tests: a Redis that is down or a URL that is not set
must turn the job red, not green with the tests skipped. Without the variable
(a laptop with no Redis) the tests skip, as before. Every package fixture that
reaches for the test Redis calls :func:`redis_unavailable`, never ``pytest.skip``
(``test_redis_required_guard.py`` holds that).
"""

from __future__ import annotations

import os
from typing import NoReturn

import pytest

REQUIRED_ENV = "SANDLOOM_REDIS_REQUIRED"


def redis_required() -> bool:
    """Whether this run must have its Redis (``SANDLOOM_REDIS_REQUIRED=1``)."""
    return os.environ.get(REQUIRED_ENV) == "1"


def redis_unavailable(reason: str) -> NoReturn:
    """Fail when the lane requires Redis, else skip, with ``reason``."""
    if redis_required():
        pytest.fail(
            f"{reason} ({REQUIRED_ENV}=1: this lane may not skip)", pytrace=False
        )
    pytest.skip(reason)
