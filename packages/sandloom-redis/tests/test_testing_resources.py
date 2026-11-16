"""Inventory / pruning / allocation of the shared test Redis indexes
(``sandloom_redis.testing.resources``, ``tools/checks/test_resources.py``).

A stale marker is pruned only when nothing is connected and its last use is
known and old; an active run (fresh stamp, live connection) never is. The
verdict itself (``sandloom.core.testing.resources.verdict``) is the kernel's and
tested with its own tests.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from _redis_required import redis_unavailable
from sandloom_redis.testing import resources
from sandloom_redis.testing.ownership import REDIS_LAST_USED_KEY, REDIS_OWNER_KEY

from sandloom.core.testing import ownership, session_checks, workers
from sandloom.core.testing.resources import Resource, verdict

BACKEND = Path(__file__).resolve().parents[3]
DAY = 86400.0


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
        redis_unavailable("Redis CACHE_URL not configured")
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
    # Leave DB 0 to new connections, which start there before SELECT.
    url = resources._server_url(_redis_url())
    owner = f"resources-test-{uuid.uuid4().hex[:8]}"
    try:
        mapping = resources.allocate_worker_redis([url], 1, owner)
    except redis.exceptions.ConnectionError as exc:
        redis_unavailable(f"Redis unreachable: {exc}")
    except ownership.TestResourceOwnershipError:
        redis_unavailable("no free Redis index on the shared test server")
    (index,) = next(iter(mapping.values()))
    try:
        yield url, index, owner
    finally:
        client = _index_client(url, index)
        try:
            if client.get(REDIS_OWNER_KEY) == owner.encode():
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
        assert client.get(REDIS_OWNER_KEY) == owner.encode()
        assert int(client.get(REDIS_LAST_USED_KEY)) >= time.time() - 60
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


def _scan_one(url: str, index: int) -> Resource:
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
        ("SET", REDIS_LAST_USED_KEY, old),
        ("SET", "sandloom:test:probe", "x"),
    )
    seen = _scan_one(url, index)
    assert (seen.owner, seen.source, seen.live) == (owner, "stamp", 0)
    assert seen.kind == "redis"
    assert verdict(seen, now=time.time())[0] is True
    # A run claims it between the scan and the prune: left alone.
    _on_index(url, index, ("SET", REDIS_LAST_USED_KEY, int(time.time())))
    assert resources.prune_redis(url, seen) is False
    assert _on_index(url, index, ("GET", "sandloom:test:probe")) == [b"x"]
    # Unchanged since a fresh scan: emptied.
    _on_index(url, index, ("SET", REDIS_LAST_USED_KEY, old))
    assert resources.prune_redis(url, _scan_one(url, index)) is True
    assert _on_index(url, index, ("DBSIZE",)) == [0]


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
            redis_unavailable("no free Redis index on the shared test server")
        (got,) = next(iter(mapping.values()))
        try:
            assert got != index
        finally:
            _on_index(url, got, ("FLUSHDB",))
    finally:
        busy.close()
        busy.connection_pool.disconnect()


def test_another_runs_scanner_is_not_a_client_of_the_index_it_reads() -> None:
    """A scan SELECTs each index in turn; a concurrent run must not take that
    connection for someone using the index (it made prunes and counts flaky).
    A fixed CLIENT LIST, not the shared server: other runs connect at will."""

    class Server:
        def execute_command(self, *args: Any) -> int:
            assert args == ("CLIENT", "ID")
            return 1

        def client_list(self) -> list[dict[str, str]]:
            return [
                {"id": "1", "db": "5", "name": ""},  # this scanner
                {"id": "2", "db": "7", "name": resources.SCANNER_CLIENT_NAME},
                {"id": "3", "db": "7", "name": ""},  # a run using index 7
                {"id": "4", "db": "9", "name": resources.SCANNER_CLIENT_NAME},
            ]

    assert resources._clients_per_db(Server()) == {7: 1}


def test_the_scanner_names_its_connection(
    scratch_index: tuple[str, int, str],
) -> None:
    url, _index, _owner = scratch_index
    scanner = resources._connect_redis(url)
    try:
        name = scanner.execute_command("CLIENT", "GETNAME")
        assert name in (
            resources.SCANNER_CLIENT_NAME,
            resources.SCANNER_CLIENT_NAME.encode(),
        )
    finally:
        scanner.close()


def test_affordable_workers_count_own_and_free_indexes_without_claiming(
    scratch_index: tuple[str, int, str],
) -> None:
    url, index, owner = scratch_index
    assert resources.affordable_redis_workers([url], owner) >= 1
    # Which indexes count, not how many: other test runs on a shared Redis
    # claim and free indexes between two counts.
    stranger = f"resources-test-{uuid.uuid4().hex[:8]}"
    base = [resources._index_of(url)]
    probe = resources._connect_redis(url)
    try:
        mine, _free = resources._claimable_indexes(probe, base, owner)
        theirs, their_free = resources._claimable_indexes(probe, base, stranger)
    finally:
        probe.close()
    assert index in mine  # the owner's own index counts for the owner
    assert index not in theirs and index not in their_free  # never for a stranger
    client = _index_client(url, index)
    try:  # read-only: the owner marker is untouched
        assert client.get(REDIS_OWNER_KEY) == owner.encode()
    finally:
        client.close()


def test_affordable_workers_divide_own_and_free_indexes_by_the_bases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The arithmetic on a fixed snapshot (the live server changes meanwhile)."""

    class Client:
        def close(self) -> None:
            pass

    snapshot = {"owner": ([3, 4], [5, 6, 7]), "stranger": ([], [5, 6, 7])}
    monkeypatch.setattr(resources, "_connect_redis", lambda _url: Client())
    monkeypatch.setattr(
        resources, "_claimable_indexes", lambda _c, _bases, owner: snapshot[owner]
    )
    urls = ["redis://h:6379/1", "redis://h:6379/2"]  # two bases share the indexes
    assert resources.affordable_redis_workers(urls, "owner") == 5 // 2
    assert resources.affordable_redis_workers(urls, "stranger") == 3 // 2
    assert resources.affordable_redis_workers([], "owner") == 0


@pytest.mark.parametrize(("room", "expected"), [(8, 6), (3, 3), (1, 0), (0, 0)])
def test_auto_workers_fall_back_when_redis_indexes_are_short(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    room: int,
    expected: int,
) -> None:
    """``sandloom test`` (``-n auto``) runs fewer workers, or serially, instead of
    aborting the session when Redis has too few free indexes."""
    from sandloom_redis.testing import isolation

    from sandloom.core.testing import connections

    values = {
        "DATABASE_URL": "postgresql://x/y",
        "INFRA": json.dumps(
            {"cache": {"default": {"url": "redis://localhost:6380/1"}}}
        ),
    }
    monkeypatch.setattr(session_checks, "run_values", lambda _config: values)
    monkeypatch.setattr(connections, "affordable_workers", lambda n, _db: n)
    monkeypatch.setattr(isolation, "redis_auto", lambda: True)
    monkeypatch.setattr(
        resources, "affordable_redis_workers", lambda _urls, _owner: room
    )
    config: Any = object()
    assert session_checks.cap_auto_workers(config, 6) == expected
    if room < 6:
        assert "Redis database indexes are free" in capsys.readouterr().err
