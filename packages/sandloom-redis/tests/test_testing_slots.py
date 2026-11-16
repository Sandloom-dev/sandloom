"""Test-run slots and the Redis indexes they claim (``sandloom.core.testing.slots``
through the Redis harness family).

Two test runs in one checkout never share Redis indexes: the second takes slot 2
and its own indexes. Real PostgreSQL and Redis: the test server of this run.
The slot's own rules (numbering, locks, the database) are tested in
``src/sandloom/core/tests/test_dev/test_dev_slots.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import uuid
from collections.abc import Iterable, Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from _redis_required import redis_unavailable
from dotenv import dotenv_values
from sandloom_redis.testing import family as redis_family
from sandloom_redis.testing import resources
from sandloom_redis.testing.ownership import REDIS_OWNER_KEY

from sandloom.core.testing import ownership, slots, workers

BACKEND = Path(__file__).resolve().parents[3]


def _test_url(var: str) -> str:
    values = workers.worker_env_values(BACKEND / ".env.test")
    return os.environ.get(var) or values.get(var) or ""


def _plain(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(scheme="postgresql", path=f"/{name}", query=""))


def _configured_redis() -> dict[str, str]:
    """This run's configured Redis URLs (``CACHE_URL`` / ``BROKER_URL``):
    ``.env.test`` with the ``SANDLOOM_TEST_*`` overrides, before any xdist
    worker rewrite — the base indexes the run's own processes use."""
    values = {
        k: v for k, v in dotenv_values(BACKEND / ".env.test").items() if v is not None
    }
    values.update(workers.explicit_test_overrides())
    return {
        var: values[var]
        for var in ("CACHE_URL", "BROKER_URL")
        if (values.get(var) or "").startswith(("redis://", "rediss://"))
    }


def _redis_owners(urls: Iterable[str]) -> dict[str, str | None]:
    """The owner marker (``None``: unmarked) of each Redis index in ``urls``."""
    redis = pytest.importorskip("redis")
    owners: dict[str, str | None] = {}
    for url in urls:
        client = redis.Redis.from_url(url, socket_connect_timeout=5)
        try:
            raw = client.get(REDIS_OWNER_KEY)
        finally:
            client.close()
        owners[url] = raw.decode() if isinstance(raw, bytes) else raw
    return owners


def _release_slot_redis(server: str, root: Path) -> list[str]:
    """Hand back every index of the Redis ``server`` a slot of the checkout
    ``root`` still holds (a holder subprocess killed mid-test, a slot that lost
    its lock), so no test leaves an index marked by a run that no longer
    exists. Returns the indexes released."""
    released: list[str] = []
    for found in resources.scan_redis(server):
        owner = found.owner or ""
        if f":{root}#" not in owner:
            continue
        index = urlsplit(found.name).path
        url = urlunsplit(urlsplit(server)._replace(path=index))
        if resources.release_redis(url, owner):
            released.append(url)
    return released


@pytest.fixture
def checkout(tmp_path: Path) -> Iterator[tuple[Path, str]]:
    """A checkout whose ``.env.test.local`` names this server's test database
    ``sandloom_test_slots_<random>`` (its slot copies dropped afterwards).

    Its Redis URLs are this run's configured ones, so a slot taken in the
    checkout allocates its own indexes and never the run's base indexes (an
    unclaimed base, under ``SANDLOOM_TEST_REDIS_DB_STRIDE=auto``, looks free).
    Afterwards every index a slot of the checkout still marks is handed back,
    and the owner of each configured index must be the one it had before (or
    this run's): a slot test never leaves another owner on the run's Redis.
    """
    psycopg2 = pytest.importorskip("psycopg2")
    base = _test_url("DATABASE_URL")
    configured = _configured_redis()
    if not base.startswith("postgresql") or "CACHE_URL" not in configured:
        redis_unavailable("PostgreSQL / Redis test URLs not configured")
    cache = configured["CACHE_URL"]
    broker = configured.get("BROKER_URL", cache)
    before = _redis_owners({cache, broker})
    name = f"sandloom_test_slots_{uuid.uuid4().hex[:8]}"
    root = tmp_path / "app"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".env.test").write_text("ENVIRONMENT=test\n")
    database = urlunsplit(urlsplit(base)._replace(path="/" + name))
    (root / ".env.test.local").write_text(
        f"SANDLOOM_TEST_DATABASE_URL={database}\n"
        f"SANDLOOM_TEST_CACHE_URL={cache}\n"
        f"SANDLOOM_TEST_BROKER_URL={broker}\n"
    )
    yield root, name
    admin = psycopg2.connect(_plain(base, "postgres"), connect_timeout=5)
    admin.autocommit = True
    with admin.cursor() as cur:
        for suffix in ("", "_2", "_3"):
            cur.execute(f'DROP DATABASE IF EXISTS "{name}{suffix}" WITH (FORCE)')
    admin.close()
    _release_slot_redis(cache, root)
    mine = ownership.current_owner()
    for url, owner in _redis_owners(before).items():
        assert owner in {before[url], mine}, (
            f"a slot test left owner {owner!r} on the run's Redis index {url} "
            f"(was {before[url]!r})"
        )


def _clean_env(home: Path) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("SANDLOOM_TEST_") and k not in {"CI", "PYTEST_XDIST_WORKER"}
    }
    env["SANDLOOM_HOME"] = str(home)
    return env


_HOLDER = textwrap.dedent(
    """
    import json, sys, time
    from pathlib import Path
    from sandloom.core.testing import slots
    slot = slots.acquire_test_slot([Path(sys.argv[1])], cwd=Path(sys.argv[1]))
    print(json.dumps({"number": slot.number, "db": slot.database_url,
                      "owner": slot.owner, "redis": slot.redis_urls}), flush=True)
    time.sleep(60)
    """
)


def test_a_second_run_in_one_checkout_gets_slot_two(
    checkout: tuple[Path, str], tmp_path: Path
) -> None:
    root, name = checkout
    env = _clean_env(tmp_path / "home")
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(root)],
        env=env,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        first = json.loads(holder.stdout.readline())
        assert first["number"] == 1
        assert first["db"].endswith(f"/{name}")
        mine = dict(env)
        slot = slots.acquire_test_slot([root], mine, cwd=root, announce=False)
        assert slot is not None
        try:
            assert slot.number == 2
            assert mine["SANDLOOM_TEST_DATABASE_URL"].endswith(f"/{name}_2")
            assert mine["SANDLOOM_TEST_OWNER"].endswith("#2")
            assert mine["SANDLOOM_TEST_OWNER"] != first["owner"]
            assert mine[slots.SLOT_HELD_ENV] == str(os.getpid())
            assert mine["SANDLOOM_TEST_REDIS_DB_STRIDE"] == "auto"
            for var in ("SANDLOOM_TEST_CACHE_URL", "SANDLOOM_TEST_BROKER_URL"):
                assert mine[var] != first["redis"][var], var
            # Every Redis index used is distinct across the two runs.
            indexes = {
                urlsplit(u).path
                for u in (*first["redis"].values(), *slot.redis_urls.values())
            }
            assert len(indexes) == 4
        finally:
            slot.release()
    finally:
        holder.kill()
        holder.wait()
    # The holder is gone: its slot is free again.
    again = dict(env)
    slot = slots.acquire_test_slot([root], again, cwd=root, announce=False)
    assert slot is not None and slot.number == 1
    slot.release()


def test_a_released_slot_hands_back_its_redis_indexes(
    checkout: tuple[Path, str], tmp_path: Path
) -> None:
    """A slot claims Redis indexes for its owner; releasing it empties them
    (only while that owner still holds them), so runs do not leak indexes."""
    redis = pytest.importorskip("redis")

    root, _ = checkout
    env = _clean_env(tmp_path / "home")
    slot = slots.acquire_test_slot([root], env, cwd=root, announce=False)
    assert slot is not None
    claimed = list(slot.claimed_urls)
    if not claimed:
        slot.release()
        pytest.skip("no Redis URLs or no free index on the shared test server")
    slot.release()
    for url in claimed:
        client = redis.Redis.from_url(url)
        try:
            # Another run may claim the index right after the release: only
            # this slot's marker must be gone.
            assert client.get(REDIS_OWNER_KEY) != slot.owner.encode()
        finally:
            client.close()


def test_a_slot_that_lost_its_lock_leaves_the_redis_indexes_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the lock is lost another run may hold the same slot, with the same
    owner and indexes: the old process must not empty them at exit."""
    released: list[str] = []
    monkeypatch.setattr(
        redis_family.resources,
        "release_redis",
        lambda u, _o: released.append(u),
    )
    slot = slots.Slot(
        number=2,
        database_url="postgresql://x/y",
        owner="m:/repo#2",
        claimed_urls=("redis://h/7",),
    )
    monkeypatch.setattr(slots, "slot_lost", lambda _slot=None: True)
    slot.release()
    assert released == []
    slot.claimed_urls = ("redis://h/7",)
    monkeypatch.setattr(slots, "slot_lost", lambda _slot=None: False)
    slot.release()
    assert released == ["redis://h/7"]
