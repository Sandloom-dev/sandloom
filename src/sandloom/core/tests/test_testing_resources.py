"""Inventory / pruning / allocation of shared test resources
(``sandloom.core.testing.resources``, ``tools/checks/test_resources.py``).

A stale marker is pruned only when nothing is connected and its last use is
known and old; an active run (fresh stamp, live connection) never is.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from sandloom.core.testing import ownership, resources, session_checks, workers
from sandloom.core.tests.test_testing_ownership import BACKEND, scratch_db  # noqa: F401

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


def test_scan_redis_disconnects_its_client_before_a_prune(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scan must not make the next prune see its own idle connection."""

    class _Pool:
        disconnected = False

        def disconnect(self) -> None:
            self.disconnected = True

    class _Client:
        def __init__(self) -> None:
            self.connection_pool = _Pool()
            self.closed = False

        def close(self) -> None:
            self.closed = True

    client = _Client()
    monkeypatch.setattr(resources, "_connect_redis", lambda _url: client)
    monkeypatch.setattr(resources, "_clients_per_db", lambda _client: {})
    monkeypatch.setattr(resources, "_database_count", lambda _client: 1)
    monkeypatch.setattr(resources, "_headers", lambda _client, _indexes: [(0, "", "")])

    assert resources.scan_redis("redis://example.test:6379/0") == []
    assert client.closed and client.connection_pool.disconnected


def _redis_url() -> str:
    values = workers.worker_env_values(BACKEND / ".env.test")
    url = os.environ.get("CACHE_URL") or values.get("CACHE_URL") or ""
    if not url.startswith("redis"):
        pytest.skip("Redis CACHE_URL not configured")
    return url


def _index_client(url: str, index: int) -> Any:
    from urllib.parse import urlsplit, urlunsplit

    import redis

    # The URL's path wins over a db= keyword in redis-py: replace the path.
    return redis.Redis.from_url(urlunsplit(urlsplit(url)._replace(path=f"/{index}")))


@pytest.fixture
def scratch_index() -> Iterator[tuple[str, int, str]]:
    """One free Redis index claimed by a throw-away owner (released after)."""
    redis = pytest.importorskip("redis")
    url = _redis_url()
    owner = f"resources-test-{uuid.uuid4().hex[:8]}"
    try:
        mapping = resources.allocate_worker_redis([url], 1, owner)
    except redis.exceptions.ConnectionError as exc:
        pytest.skip(f"Redis unreachable: {exc}")
    except ownership.TestResourceOwnershipError:
        pytest.skip("no free Redis index on the shared test server")
    (index,) = next(iter(mapping.values()))
    try:
        yield url, index, owner
    finally:
        client = _index_client(url, index)
        try:
            if client.get(ownership.REDIS_OWNER_KEY) == owner.encode():
                client.flushdb()
        finally:
            client.close()


def test_allocation_claims_free_indexes_and_reuses_its_own(
    scratch_index: tuple[str, int, str],
) -> None:

    url, index, owner = scratch_index
    base = int(url.rsplit("/", 1)[1])
    assert index != base
    client = _index_client(url, index)
    try:
        assert client.get(ownership.REDIS_OWNER_KEY) == owner.encode()
        assert int(client.get(ownership.REDIS_LAST_USED_KEY)) >= time.time() - 60
    finally:
        client.close()
    # A rerun of the same owner gets the same index back first.
    again = resources.allocate_worker_redis([url], 1, owner)
    assert again == {base: [index]}


def _on_index(url: str, index: int, *commands: tuple[Any, ...]) -> list[Any]:
    """Run ``commands`` on one index and DISCONNECT (a connected client marks
    the index as in use, which the scan rightly refuses to prune)."""
    client = _index_client(url, index)
    try:
        return [client.execute_command(*command) for command in commands]
    finally:
        client.close()
        client.connection_pool.disconnect()


def _scan_one(url: str, index: int) -> resources.Resource:
    (seen,) = [r for r in resources.scan_redis(url) if r.name.endswith(f"/{index}")]
    return seen


def test_scan_and_prune_a_stale_index_but_never_a_changed_one(
    scratch_index: tuple[str, int, str],
) -> None:
    url, index, owner = scratch_index
    old = int(time.time() - 30 * DAY)
    _on_index(
        url,
        index,
        ("SET", ownership.REDIS_LAST_USED_KEY, old),
        ("SET", "sandloom:test:probe", "x"),
    )
    seen = _scan_one(url, index)
    assert (seen.owner, seen.source, seen.live) == (owner, "stamp", 0)
    assert resources.verdict(seen, now=time.time())[0] is True
    # A run claims it between the scan and the prune: left alone.
    _on_index(url, index, ("SET", ownership.REDIS_LAST_USED_KEY, int(time.time())))
    assert resources.prune(url, seen) is False
    assert _on_index(url, index, ("GET", "sandloom:test:probe")) == [b"x"]
    # Unchanged since a fresh scan: emptied.
    _on_index(url, index, ("SET", ownership.REDIS_LAST_USED_KEY, old))
    assert resources.prune(url, _scan_one(url, index)) is True
    assert _on_index(url, index, ("DBSIZE",)) == [0]


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


def test_allocation_never_hands_out_an_index_someone_is_connected_to(
    scratch_index: tuple[str, int, str],
) -> None:
    """A run that FLUSHDBs its own index also drops its owner marker; the
    index looks free but is still in use while that run is connected."""
    url, index, _owner = scratch_index
    busy = _index_client(url, index)
    try:
        busy.flushdb()  # empty and unmarked, but connected
        other = f"resources-test-{uuid.uuid4().hex[:8]}"
        try:
            mapping = resources.allocate_worker_redis([url], 1, other)
        except ownership.TestResourceOwnershipError:
            pytest.skip("no free Redis index on the shared test server")
        (got,) = next(iter(mapping.values()))
        try:
            assert got != index
        finally:
            _on_index(url, got, ("FLUSHDB",))
    finally:
        busy.close()
        busy.connection_pool.disconnect()


def test_affordable_workers_count_own_and_free_indexes_without_claiming(
    scratch_index: tuple[str, int, str],
) -> None:
    url, index, owner = scratch_index
    room = resources.affordable_redis_workers([url], owner)
    assert room >= 1  # at least the index this owner already holds
    stranger = f"resources-test-{uuid.uuid4().hex[:8]}"
    assert resources.affordable_redis_workers([url], stranger) <= room - 1
    client = _index_client(url, index)
    try:  # read-only: the owner marker is untouched
        assert client.get(ownership.REDIS_OWNER_KEY) == owner.encode()
    finally:
        client.close()


@pytest.mark.parametrize(("room", "expected"), [(8, 6), (3, 3), (1, 0), (0, 0)])
def test_auto_workers_fall_back_when_redis_indexes_are_short(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    room: int,
    expected: int,
) -> None:
    """``sandloom test`` (``-n auto``) runs fewer workers, or serially, instead of
    aborting the session when Redis has too few free indexes."""
    from sandloom.core.testing import connections, session_checks, workers

    values = {
        "DATABASE_URL": "postgresql://x/y",
        "CACHE_URL": "redis://localhost:6380/1",
    }
    monkeypatch.setattr(session_checks, "run_values", lambda _config: values)
    monkeypatch.setattr(connections, "affordable_workers", lambda n, _db: n)
    monkeypatch.setattr(workers, "redis_auto", lambda: True)
    monkeypatch.setattr(
        resources, "affordable_redis_workers", lambda _urls, _owner: room
    )
    config: Any = object()
    assert session_checks.cap_auto_workers(config, 6) == expected
    if room < 6:
        assert "Redis database indexes are free" in capsys.readouterr().err


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
