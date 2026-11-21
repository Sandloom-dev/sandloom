"""The Redis harness family: the hook protocol, the entry point that registers it,
and what each hook does on a process that cannot reach a Redis.

The harness itself (the loader, the duplicate and broken-family refusals, the
callers of the hooks) is tested with fake families in
``src/sandloom/core/tests/test_testing_families.py``; this file runs the real one.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import pytest
from sandloom_redis.testing import family as redis_family
from sandloom_redis.testing.isolation import REDIS_STRIDE_ENV

from sandloom.core.testing import families, slots
from sandloom.core.testing.families import HarnessFamily

_CACHE = {"SANDLOOM_TEST_CACHE_URL": "redis://h/1"}


def _slot() -> slots.Slot:
    return slots.Slot(number=1, database_url="postgresql://x/y", owner="o#1")


@pytest.fixture
def only(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Make exactly these families the harness's (no discovery)."""

    def use(*members: HarnessFamily) -> None:
        monkeypatch.setattr(families, "_loaded", tuple(members))

    return use


def test_the_redis_family_satisfies_the_protocol() -> None:
    assert isinstance(redis_family.FAMILY, HarnessFamily)
    assert redis_family.FAMILY.name == "redis"


def test_the_package_registers_the_redis_family_in_the_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read from the installed metadata (``packages/sandloom-redis/pyproject.toml``):
    a stale install without the entry point fails here, not silently."""
    monkeypatch.setattr(families, "_loaded", None)
    assert [f for f in families.harness_families() if f.name == "redis"] == [
        redis_family.FAMILY
    ]


def test_the_redis_family_is_a_no_op_without_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(redis_family, "redis_client_installed", lambda: False)
    family = redis_family.FAMILY
    values = {"CACHE_URL": "redis://h:6/1"}
    assert family.rewrite_worker(values, {}, "gw0", 0) == {}
    assert family.slot_bases(values, "o", {}) == {}
    assert family.cap_workers(values, 4) == 4
    family.allocate_workers(values, 2)  # nothing to allocate: returns


def test_the_redis_family_is_a_no_op_without_a_redis_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client is installed but nothing points at a Redis: no worker rewrite."""
    monkeypatch.setattr(redis_family, "redis_client_installed", lambda: True)
    values = {"CACHE_URL": "memory://", "DATABASE_URL": "postgresql://h/db"}
    assert redis_family.FAMILY.rewrite_worker(values, {}, "gw0", 0) == {}


def _own_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Redis server grants index 7 to base index 1, without a server."""
    monkeypatch.setattr(
        redis_family.resources,
        "allocate_worker_redis",
        lambda urls, workers, owner, **_: {1: [7]},
    )


def test_the_slot_claims_own_indexes_and_asks_for_auto_stride(
    only: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(redis_family, "redis_client_installed", lambda: True)
    _own_index(monkeypatch)
    env: dict[str, str] = {}
    claimed = redis_family.FAMILY.slot_bases(_CACHE, "o#1", env)
    assert claimed == {"SANDLOOM_TEST_CACHE_URL": "redis://h/7"}
    assert env == {REDIS_STRIDE_ENV: "auto"}
    # the same through the slot, with the Redis family as the harness's only one
    only(redis_family.FAMILY)
    through_slot: dict[str, str] = {}
    slots._point_at(_slot(), dict(_CACHE), through_slot, False)
    assert through_slot[REDIS_STRIDE_ENV] == "auto"
    assert through_slot["SANDLOOM_TEST_CACHE_URL"] == "redis://h/7"


@pytest.mark.parametrize(
    ("url", "owned"),
    [
        ("redis://h/1", True),
        ("rediss://h:6380/1", True),
        ("amqp://h/1", False),
        ("memory://", False),
        ("", False),
    ],
)
def test_the_family_owns_the_redis_urls_only(url: str, owned: bool) -> None:
    assert redis_family.FAMILY.owns_url(url) is owned


@pytest.mark.parametrize(
    ("stated", "base", "same"),
    [
        ("redis://h:6380/0", "redis://h:6380", True),  # no path is database 0
        ("redis://h:6380/0/", "redis://h:6380/", True),
        ("redis://h:6380/00", "redis://h:6380/0", True),  # same index, as slot_bases
        ("redis://h:6380/01", "redis://h:6380/1", True),
        ("redis://h:6380/x", "redis://h:6380/x", True),
        # redis-py reads a path that is no number as database 0
        ("redis://h:6380/x", "redis://h:6380/0", True),
        # and drops every '/' and decodes the path
        ("redis://h:6380/%33", "redis://h:6380/3", True),
        ("redis://h:6380/1/5", "redis://h:6380/15", True),
        ("redis://h:6380/1/5", "redis://h:6380/1", False),
        ("redis://h:6380/1", "redis://h:6380/2", False),
        ("redis://h:6380/1", "redis://other:6380/1", False),
        ("redis://h:6380/1", "rediss://h:6380/1", False),
        ("redis://h:6380/1?x=1", "redis://h:6380/1", False),
        # the host is case-insensitive; a trailing dot makes an absolute name
        # that the resolver may not complete the way the other one is: different
        ("redis://Redis.EXAMPLE:6380/1", "redis://redis.example:6380/1", True),
        ("redis://redis.example.:6380/1", "redis://redis.example:6380/1", False),
        ("redis://redis.example.:6380/1", "redis://redis.example.:6380/1", True),
        ("redis://redis.example:6380/1", "redis://other.example:6380/1", False),
        # no port is 6379
        ("redis://h/1", "redis://h:6379/1", True),
        ("redis://h:6379", "redis://H/0", True),
        ("redis://h:6379", "redis://H./0", False),
        ("redis://h/1", "redis://h:6380/1", False),
        # credentials stay part of the identity, and case-sensitive
        ("redis://:Pw@h/1", "redis://:pw@h/1", False),
        ("redis://:pw@H/1", "redis://:pw@h:6379/1", True),
        # redis-py reads ?db=N over the path
        ("redis://h/0?db=3", "redis://h/3", True),
        ("redis://h/5?db=3", "redis://h/3", True),
        ("redis://h/3?db=5", "redis://h/3", False),
        ("redis://h?db=3", "redis://h/3?db=03", True),
        (
            "redis://h/3?db=",
            "redis://h/3",
            True,
        ),  # an empty db is ignored, as redis-py does
        ("redis://h/3?db=5&x=1", "redis://h/5?x=1", True),
        ("redis://h/3?db=5&x=1", "redis://h/5", False),
    ],
)
def test_the_family_names_the_same_database(stated: str, base: str, same: bool) -> None:
    assert redis_family.FAMILY.same_database(stated, base) is same


_SECRET = "S3CRETpw"


@pytest.mark.parametrize(
    "url",
    [
        "redis://h:99999/1",  # port out of range
        "redis://h:notaport/1",
        f"redis://:{_SECRET}/cd@h:6379/1",  # an unencoded '/' in the password
        f"redis://:{_SECRET}?x@h:6379/1",  # an unencoded '?'
        "redis://::1/1",  # an IPv6 host without brackets
        # a character that NFKC-normalises to '#': urlsplit refuses the netloc
        f"redis://user:{_SECRET}@host{chr(0xFF03)}x:6379/1",
        "memory://",
    ],
)
def test_a_url_that_names_no_endpoint_is_the_same_as_nothing_never_an_error(
    url: str,
) -> None:
    """``redis_endpoint`` reads none of these: ``False`` against any URL, itself
    included, and no error that could quote the password."""
    for other in (url, "redis://h:6379/1", "redis://h:99999/1"):
        assert redis_family.FAMILY.same_database(url, other) is False
        assert redis_family.FAMILY.same_database(other, url) is False


def test_a_parallel_run_asks_for_allocated_indexes_when_it_can_reach_a_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(redis_family, "redis_client_installed", lambda: True)
    assert redis_family.FAMILY.parallel_env() == {REDIS_STRIDE_ENV: "auto"}
    monkeypatch.setattr(redis_family, "redis_client_installed", lambda: False)
    assert redis_family.FAMILY.parallel_env() == {}


def test_an_explicit_stride_is_not_replaced_by_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(redis_family, "redis_client_installed", lambda: True)
    _own_index(monkeypatch)
    env = {REDIS_STRIDE_ENV: "32"}
    redis_family.FAMILY.slot_bases(_CACHE, "o#1", env)
    assert env == {REDIS_STRIDE_ENV: "32"}


@pytest.mark.parametrize(
    ("client", "urls"),
    [
        (False, _CACHE),  # a Redis URL but no client: this run reaches no Redis
        (True, {"SANDLOOM_TEST_CACHE_URL": "memory://"}),  # a client, no Redis URL
        (True, {}),
    ],
)
def test_the_stride_is_left_alone_without_a_plain_redis_url_and_a_client(
    monkeypatch: pytest.MonkeyPatch, client: bool, urls: dict[str, str]
) -> None:
    monkeypatch.setattr(redis_family, "redis_client_installed", lambda: client)

    def refuse(*_: object, **__: object) -> dict[int, list[int]]:
        raise AssertionError("an index was claimed for a process without Redis")

    monkeypatch.setattr(redis_family.resources, "allocate_worker_redis", refuse)
    env: dict[str, str] = {}
    assert redis_family.FAMILY.slot_bases(urls, "o#1", env) == {}
    assert env == {}


@pytest.mark.asyncio
async def test_the_redis_family_flushes_only_a_provider_that_hands_out_a_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flushed: list[object] = []

    async def flush(client: object, **_: object) -> None:
        flushed.append(client)

    monkeypatch.setattr(redis_family, "flush_owned_redis", flush)

    class Cache:
        async def redis_client(self) -> object:
            return "the-client"

    assert await redis_family.FAMILY.flush_owned(object()) is False
    assert flushed == []
    assert await redis_family.FAMILY.flush_owned(Cache()) is True
    assert flushed == ["the-client"]


def test_loading_the_family_imports_no_settings_and_no_sqlalchemy() -> None:
    """The harness loads a family before the settings snapshot (`apply_worker_env`
    runs ahead of every settings import): the family and the kernel half it needs
    (`sandloom.driverkit.testing_family`) must not import the settings, SQLAlchemy,
    the infra layer or the eventing layer. A fresh interpreter, so what this
    process already imported does not count."""
    code = (
        "import json, sys; import sandloom_redis.testing.family; "
        'print(json.dumps([m for m in ("sandloom.core.config.settings", "sqlalchemy", '
        '"sandloom.core.infra.config", "sandloom.core.eventing") if m in sys.modules]))'
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == []
