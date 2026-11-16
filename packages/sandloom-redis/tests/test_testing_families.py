"""The Redis harness family: the hook protocol, the entry point that registers it,
and what each hook does on a process that cannot reach a Redis.

The harness itself (the loader, the duplicate and broken-family refusals, the
callers of the hooks) is tested with fake families in
``src/sandloom/core/tests/test_testing_families.py``; this file runs the real one.
"""

from __future__ import annotations

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
