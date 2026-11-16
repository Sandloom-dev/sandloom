"""Inventory / pruning / allocation of shared test resources
(``sandloom.core.testing.resources``, ``tools/checks/test_resources.py``).

A stale marker is pruned only when nothing is connected and its last use is
known and old; an active run (fresh stamp, live connection) never is.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from sandloom.core.testing import ownership, resources, session_checks
from sandloom.core.tests.test_testing_ownership import scratch_db  # noqa: F401

NOW = 1_800_000_000.0
DAY = 86400.0


def _resource(**kw: Any) -> resources.Resource:
    base = {
        "kind": "redis",
        "name": "redis://h:1/3",
        "owner": "old-run",
        "last_used": NOW - 10 * DAY,
        "source": "stamp",
        "live": 0,
        "size": 1,
    }
    return resources.Resource(**{**base, **kw})


def test_only_an_old_disconnected_marked_resource_is_stale() -> None:
    assert resources.verdict(_resource(), now=NOW)[0] is True
    assert resources.verdict(_resource(live=1), now=NOW) == (
        False,
        "in use (1 connected)",
    )
    assert resources.verdict(_resource(last_used=NOW - DAY), now=NOW)[0] is False
    assert resources.verdict(_resource(last_used=None), now=NOW)[1] == (
        "last use unknown"
    )
    assert resources.verdict(_resource(owner=None), now=NOW)[0] is False
    assert resources.verdict(_resource(owner=None), now=NOW, include_unmarked=True)[0]
    protected = resources.verdict(_resource(), now=NOW, protect_owners=["old-run"])
    assert protected == (False, "protected owner")


def test_the_age_threshold_never_drops_below_a_day() -> None:
    hours_old = _resource(last_used=NOW - 3600)
    assert resources.verdict(hours_old, now=NOW, max_age_days=0)[0] is False


def test_prune_refuses_a_resource_that_is_not_a_database() -> None:
    """A Redis index is pruned by its backend's package, never silently skipped."""
    with pytest.raises(ValueError, match="only databases"):
        resources.prune("redis://h:1/0", _resource())  # kind "redis"


def test_a_database_is_stamped_on_claim_and_never_dropped_while_connected(
    scratch_db: str,  # noqa: F811 - the fixture
) -> None:
    import psycopg2  # type: ignore[import-untyped]

    ownership._claimed.clear()
    ownership.claim_database(scratch_db, owner="old-run")
    name = scratch_db.rsplit("/", 1)[1]
    (seen,) = [r for r in resources.scan_databases(scratch_db) if r.name == name]
    assert seen.owner == "old-run"
    assert seen.source == "stamp"
    assert seen.last_used is not None and seen.last_used >= time.time() - 60
    assert resources.verdict(seen, now=time.time())[0] is False  # fresh
    stale = resources.Resource(**{**seen.__dict__, "last_used": 0.0})
    assert resources.verdict(stale, now=time.time())[0] is True
    dsn = scratch_db.replace("postgresql+asyncpg", "postgresql")
    conn = psycopg2.connect(dsn)
    try:
        assert resources.prune(scratch_db, stale) is False  # someone is connected
    finally:
        conn.close()
    # PostgreSQL ends the backend asynchronously after the client closes:
    # wait until the scan no longer sees it connected before pruning.
    deadline = time.monotonic() + 10
    while any(r.live for r in resources.scan_databases(scratch_db) if r.name == name):
        assert time.monotonic() < deadline, "the closed connection never ended"
        time.sleep(0.05)
    assert resources.prune(scratch_db, stale) is True
    assert name not in {r.name for r in resources.scan_databases(scratch_db)}


@pytest.mark.parametrize(
    ("url", "risky"),
    [
        ("redis://localhost:56398/0", True),
        ("postgresql+asyncpg://u:p@localhost:49152/db", True),
        ("redis://localhost:6380/0", False),
        ("redis://127.0.0.1:56398/0", False),
        ("redis://cache.internal:56398/0", False),
        ("postgresql+asyncpg://u@localhost:55000/db?host=/tmp/pg", False),
        ("postgresql+asyncpg://u@localhost:55000/db?host=localhost", True),
        ("", False),
    ],
)
def test_a_url_that_can_connect_to_itself_is_found(url: str, risky: bool) -> None:
    assert bool(session_checks.self_connecting_urls({"CACHE_URL": url})) is risky
