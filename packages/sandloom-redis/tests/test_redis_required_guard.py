"""``SANDLOOM_REDIS_REQUIRED=1`` turns a missing test Redis into a failure.

The ``redis-drivers`` CI job is the only one that runs these tests, so a Redis
that is not reachable there must fail it, not skip every test and stay green.
Locally, without the variable, the same fixtures skip.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from _redis_required import (
    REQUIRED_ENV,
    cache_url_or_unavailable,
    redis_unavailable,
    require_redis,
)

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
#: A port nothing listens on: the connection is refused at once.
_DEAD_URL = "redis://127.0.0.1:1/0"
#: Package tests whose fixtures reach for the test Redis, by the cache url
#: they read (``cache_url_or_unavailable``): the cache fixture (unreachable) and the loop-affinity test.
_NEEDING_REDIS = (
    "test_redis_cache_retry_policy.py::test_incr_whose_reply_was_lost_counts_once",
    "test_redis_cache_bounded.py::test_windowed_counter_counts_ttl_and_previous",
    "test_redis_cache_loop_affinity.py::test_real_redis_client_survives_two_loops",
    "test_redis_broadcast.py::test_the_prefix_keeps_two_instances_apart",
    "test_redis_periodic_slots.py::test_ten_minutely_slots_create_one_lease_name_on_redis",
    "test_atomic_adopt_redis_identity.py::test_strict_profile_attests_real_redis_identity",
    "test_revocation_freshness.py::test_block_applies_on_next_request_even_on_budget_routes",
)


def test_the_helper_reads_the_redis_lane_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The binding to ``SANDLOOM_REDIS_REQUIRED``; the skip-or-fail rule itself
    is the driverkit guard's, tested in ``test_driverkit_required.py``. A skip
    must turn this red, not mark the test skipped: catch both outcomes."""

    def outcome() -> BaseException:
        try:
            redis_unavailable("no redis")
        except (pytest.fail.Exception, pytest.skip.Exception) as caught:
            return caught
        raise AssertionError("redis_unavailable returned")

    monkeypatch.delenv(REQUIRED_ENV, raising=False)
    assert isinstance(outcome(), pytest.skip.Exception)
    monkeypatch.setenv("SANDLOOM_CELERY_REQUIRED", "1")
    assert isinstance(outcome(), pytest.skip.Exception)
    monkeypatch.setenv(REQUIRED_ENV, "1")
    failed = outcome()
    assert isinstance(failed, pytest.fail.Exception)
    assert "no redis" in str(failed)
    assert REQUIRED_ENV in str(failed)


def _outcome(test: str, *, required: bool) -> str:
    """Run one package test in a fresh pytest against a dead Redis; its summary line."""
    env = {k: v for k, v in os.environ.items() if k != REQUIRED_ENV}
    # The run's own overrides each need the instance of ``INFRA`` they replace
    # (the harness refuses an override it cannot place): state both, dead.
    env.update(
        INFRA=json.dumps(
            {
                "cache": {"default": {"url": _DEAD_URL}},
                "taskqueue": {"default": {"broker": _DEAD_URL}},
            }
        ),
        SANDLOOM_TEST_CACHE_URL=_DEAD_URL,
        SANDLOOM_TEST_BROKER_URL=_DEAD_URL,
    )
    if required:
        env[REQUIRED_ENV] = "1"
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(HERE / test),
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    return done.stdout + done.stderr


@pytest.mark.parametrize(
    "url",
    ["", "redis://user:s3cret-pw@127.0.0.1:1/0"],
    ids=["unset", "dead-with-password"],
)
async def test_require_redis_names_no_url_and_handles_an_unset_one(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unset URL is unavailable too, and the failure reason never carries
    the URL (it can hold credentials)."""
    monkeypatch.setenv(REQUIRED_ENV, "1")
    with pytest.raises(pytest.fail.Exception) as caught:
        await require_redis(url)
    assert "s3cret-pw" not in str(caught.value)
    assert "redis://" not in str(caught.value)


def test_an_unconfigured_cache_url_skips_locally_and_fails_in_the_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``INFRA`` cache url: ``cache_url_or_unavailable`` is unavailable (a
    skip, a failure in the lane), never a bare ``RuntimeError`` and never an
    empty url handed to a driver. ``CACHE_URL`` is a removed setting and names
    no url here."""
    monkeypatch.delenv("INFRA", raising=False)
    monkeypatch.setenv("CACHE_URL", _DEAD_URL)
    monkeypatch.delenv(REQUIRED_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception):
        cache_url_or_unavailable()
    monkeypatch.setenv(REQUIRED_ENV, "1")
    with pytest.raises(pytest.fail.Exception, match="no test cache url"):
        cache_url_or_unavailable()
    monkeypatch.setenv("INFRA", json.dumps({"cache": {"default": {"url": _DEAD_URL}}}))
    assert cache_url_or_unavailable() == _DEAD_URL


@pytest.mark.parametrize("test", _NEEDING_REDIS)
def test_an_unreachable_redis_skips_locally_and_fails_in_the_lane(test: str) -> None:
    """The real fixtures, against a dead URL: skipped without the variable,
    failed with it (the job's setting)."""
    local = _outcome(test, required=False)
    assert "1 skipped" in local and "failed" not in local, local[-1500:]
    lane = _outcome(test, required=True)
    # a failing fixture is an error, a failing test body a failure
    assert re.search(r"\b1 (failed|error)\b", lane), lane[-1500:]
    assert "skipped" not in lane, lane[-1500:]
    assert "may not skip" in lane, lane[-1500:]


def test_no_package_test_skips_on_redis_without_the_helper() -> None:
    """A fixture added later that calls ``pytest.skip`` would hide a missing
    Redis from the lane: only the helper may skip."""
    offenders = []
    for path in sorted(HERE.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders += [
            f"{path.name}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "skip"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "pytest"
        ]
    assert not offenders, (
        f"use redis_unavailable() from _redis_required, not pytest.skip: {offenders}"
    )
