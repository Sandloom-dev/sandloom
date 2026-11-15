"""Harness families (``sandloom.core.testing.families``): the hook protocol, the
entry-point group ``sandloom.testing``, and that the harness calls the hooks.

The Redis family is the built-in provider until it moves into ``sandloom-redis``;
these tests run it as a family and run the harness without any.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterable, Mapping, MutableMapping
from importlib import metadata
from typing import Any

import pytest

from sandloom.core.testing import (
    families,
    ownership,
    redis_family,
    session_checks,
    slots,
    workers,
)
from sandloom.core.testing.families import HarnessFamily, HarnessFamilyError


class FakeFamily:
    """A complete family that records what the harness asks of it."""

    def __init__(self, name: str = "fake", cap: int | None = None) -> None:
        self.name = name
        self.cap = cap
        self.calls: list[tuple[str, Any]] = []

    def rewrite_worker(
        self,
        values: Mapping[str, str | None],
        environ: Mapping[str, str],
        worker_id: str,
        number: int,
    ) -> dict[str, str]:
        self.calls.append(("rewrite_worker", (worker_id, number)))
        return {"FAKE_URL": f"fake://{worker_id}"}

    def claim(self, values: Mapping[str, str]) -> None:
        self.calls.append(("claim", dict(values)))

    def slot_bases(
        self,
        urls: Mapping[str, str],
        owner: str,
        environ: MutableMapping[str, str],
    ) -> dict[str, str]:
        self.calls.append(("slot_bases", owner))
        return {var: f"{url}/own" for var, url in urls.items()}

    def release_slot_bases(self, urls: Iterable[str], owner: str) -> None:
        self.calls.append(("release_slot_bases", (tuple(urls), owner)))

    def allocate_workers(self, values: Mapping[str, str], count: int) -> None:
        self.calls.append(("allocate_workers", count))

    def cap_workers(self, values: Mapping[str, str], count: int) -> int:
        self.calls.append(("cap_workers", count))
        return count if self.cap is None else self.cap

    async def flush_owned(self, provider: object) -> bool:
        self.calls.append(("flush_owned", provider))
        return True


class Incomplete:
    """Names itself but lacks the hooks."""

    name = "incomplete"

    def claim(self, values: Mapping[str, str]) -> None: ...


def _install(
    monkeypatch: pytest.MonkeyPatch,
    entries: Mapping[str, object],
    *,
    broken: Iterable[str] = (),
) -> None:
    """Register ``entries`` (name -> loaded object) in the group, as an installed
    package would; ``broken`` names are registered with a module that does not
    exist."""
    points = []
    for name, obj in entries.items():
        module = types.ModuleType(f"_family_pkg_{name}")
        module.FAMILY = obj  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, module.__name__, module)
        points.append(
            metadata.EntryPoint(
                name, f"{module.__name__}:FAMILY", families.TESTING_GROUP
            )
        )
    for name in broken:
        points.append(
            metadata.EntryPoint(
                name, f"_no_such_pkg_{name}:FAMILY", families.TESTING_GROUP
            )
        )
    real = metadata.entry_points

    def entry_points() -> Any:
        everything = [*real(), *points]
        return metadata.EntryPoints(everything)

    monkeypatch.setattr(metadata, "entry_points", entry_points)
    monkeypatch.setattr(families, "_loaded", None)


@pytest.fixture
def only(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Make exactly these families the harness's (no discovery)."""

    def use(*members: HarnessFamily) -> None:
        monkeypatch.setattr(families, "_loaded", tuple(members))

    return use


# --------------------------------------------------------------------------- #
# The protocol and the loader
# --------------------------------------------------------------------------- #


def test_the_redis_family_and_a_fake_satisfy_the_protocol() -> None:
    assert isinstance(redis_family.FAMILY, HarnessFamily)
    assert isinstance(FakeFamily(), HarnessFamily)
    assert not isinstance(Incomplete(), HarnessFamily)


def test_the_kernel_package_registers_the_redis_family_in_the_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read from the installed metadata (``packages/sandloom/pyproject.toml``):
    a stale install without the entry point fails here, not silently."""
    monkeypatch.setattr(families, "_loaded", None)
    assert [f for f in families.harness_families() if f.name == "redis"] == [
        redis_family.FAMILY
    ]


def test_a_family_of_an_installed_package_is_loaded_from_the_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeFamily("fake")
    _install(monkeypatch, {"fake": fake})
    loaded = families.harness_families()
    assert fake in loaded
    assert "redis" in {f.name for f in loaded}


def test_a_family_that_names_itself_otherwise_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, {"registered_as": FakeFamily("named-otherwise")})
    with pytest.raises(HarnessFamilyError, match="names itself"):
        families.harness_families()


def test_a_family_without_every_hook_is_refused_not_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, {"incomplete": Incomplete()})
    with pytest.raises(HarnessFamilyError, match="does not implement HarnessFamily"):
        families.harness_families()


def test_a_family_that_fails_to_import_is_refused_not_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, {}, broken=["gone"])
    with pytest.raises(HarnessFamilyError, match="failed to import"):
        families.harness_families()


# --------------------------------------------------------------------------- #
# The harness calls the hooks
# --------------------------------------------------------------------------- #


def test_the_worker_rewrite_asks_every_family(only: Any) -> None:
    fake = FakeFamily()
    only(fake)
    env = {workers.WORKER_ENV: "gw3"}
    out = workers.rewrite_for_worker({"DATABASE_URL": "postgresql://h/db"}, env)
    assert out["FAKE_URL"] == "fake://gw3"
    assert out["DATABASE_URL"] == "postgresql://h/db_gw3"
    assert fake.calls == [("rewrite_worker", ("gw3", 3))]


def test_without_a_family_a_worker_gets_only_its_database(only: Any) -> None:
    """The DB-only harness: a Redis setting passes through untouched."""
    only()
    env = {workers.WORKER_ENV: "gw1"}
    values = {"DATABASE_URL": "postgresql://h/db", "CACHE_URL": "redis://h:6/0"}
    assert workers.rewrite_for_worker(values, env) == {
        "DATABASE_URL": "postgresql://h/db_gw1"
    }


def test_claiming_the_resources_of_a_run_asks_every_family(
    only: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeFamily()
    only(fake)
    monkeypatch.setattr(ownership, "claim_database", lambda url: None)
    values = {"DATABASE_URL": "postgresql://h/db", "CACHE_URL": "redis://h:6/1"}
    ownership.claim_test_resources(values)
    assert fake.calls == [("claim", values)]


def test_a_refusal_of_a_family_reaches_the_caller(
    only: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Refusing(FakeFamily):
        def claim(self, values: Mapping[str, str]) -> None:
            raise ownership.TestResourceOwnershipError("another run's")

    only(Refusing())
    with pytest.raises(ownership.TestResourceOwnershipError):
        ownership.claim_test_resources({})


def test_the_controller_allocates_and_caps_through_the_families(
    only: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandloom.core.testing import connections

    fake = FakeFamily(cap=2)
    only(fake)
    values = {"DATABASE_URL": "postgresql://x/y"}
    monkeypatch.setattr(session_checks, "run_values", lambda _config: values)
    monkeypatch.setattr(connections, "affordable_workers", lambda n, _db: n)
    session_checks.allocate_worker_resources(values, 4)
    config: Any = object()
    assert session_checks.cap_auto_workers(config, 6) == 2
    assert fake.calls == [("allocate_workers", 4), ("cap_workers", 6)]


def test_no_family_leaves_the_worker_count_to_the_database(
    only: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandloom.core.testing import connections

    only()
    monkeypatch.setattr(session_checks, "run_values", lambda _config: {})
    monkeypatch.setattr(connections, "affordable_workers", lambda n, _db: n - 1)
    config: Any = object()
    assert session_checks.cap_auto_workers(config, 6) == 5


def test_a_slot_takes_its_bases_from_the_families_and_hands_them_back(
    only: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeFamily()
    only(fake)
    env: dict[str, str] = {}
    claimed = slots._claim_family_bases(
        {"SANDLOOM_TEST_CACHE_URL": "redis://h/1"}, "o#1", env
    )
    assert claimed == {"SANDLOOM_TEST_CACHE_URL": "redis://h/1/own"}
    slot = slots.Slot(
        number=1,
        database_url="postgresql://x/y",
        owner="o#1",
        claimed_urls=("redis://h/1/own",),
    )
    monkeypatch.setattr(slots, "slot_lost", lambda _slot=None: False)
    slot.release()
    assert ("release_slot_bases", (("redis://h/1/own",), "o#1")) in fake.calls


# --------------------------------------------------------------------------- #
# The guard against a silent loss of isolation
# --------------------------------------------------------------------------- #


def test_a_client_without_its_family_is_refused(only: Any) -> None:
    only()
    with pytest.raises(HarnessFamilyError, match="no harness family 'redis'"):
        families.require_for_installed({"redis": True})


def test_a_client_with_its_family_or_without_the_client_is_fine(only: Any) -> None:
    only(FakeFamily("redis"))
    families.require_for_installed({"redis": True})
    only()
    families.require_for_installed({"redis": False})


# --------------------------------------------------------------------------- #
# The Redis family on a process that cannot reach a Redis
# --------------------------------------------------------------------------- #


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


@pytest.mark.asyncio
async def test_the_redis_family_flushes_only_a_provider_that_hands_out_a_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flushed: list[object] = []

    async def flush(client: object, **_: object) -> None:
        flushed.append(client)

    monkeypatch.setattr(ownership, "flush_owned_redis", flush)

    class Cache:
        async def redis_client(self) -> object:
            return "the-client"

    assert await redis_family.FAMILY.flush_owned(object()) is False
    assert flushed == []
    assert await redis_family.FAMILY.flush_owned(Cache()) is True
    assert flushed == ["the-client"]
