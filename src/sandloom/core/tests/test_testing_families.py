"""Harness families (``sandloom.core.testing.families``): the hook protocol, the
entry-point group ``sandloom.testing``, and that the harness calls the hooks.

These tests run the harness with fake families and without any; the Redis family
the ``sandloom-redis`` package registers is tested in
``packages/sandloom-redis/tests/test_testing_families.py``.
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
    family_seams,
    ownership,
    plugin,
    session_checks,
    slots,
    workers,
)
from sandloom.core.testing.families import HarnessFamily, HarnessFamilyError
from sandloom.core.testing.fixtures import flush_owned_cache


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
    _patch_entry_points(monkeypatch, points)


def _patch_entry_points(
    monkeypatch: pytest.MonkeyPatch, points: list[metadata.EntryPoint]
) -> None:
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


def test_a_fake_satisfies_the_protocol_and_an_incomplete_one_does_not() -> None:
    assert isinstance(FakeFamily(), HarnessFamily)
    assert not isinstance(Incomplete(), HarnessFamily)


def test_the_kernel_package_registers_no_family_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Redis family belongs to ``sandloom-redis``: an entry of the kernel's
    distribution in the group would make the package's own a duplicate."""
    monkeypatch.setattr(families, "_loaded", None)
    own = [
        ep
        for ep in metadata.entry_points(group=families.TESTING_GROUP)
        if ep.dist is not None and ep.dist.name == "sandloom"
    ]
    assert own == []


def test_a_family_of_an_installed_package_is_loaded_from_the_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeFamily("fake")
    _install(monkeypatch, {"fake": fake})
    assert fake in families.harness_families()


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


def _entry(name: str, module: str, dist: str) -> metadata.EntryPoint:
    point = metadata.EntryPoint(name, f"{module}:FAMILY", families.TESTING_GROUP)
    return point._for(types.SimpleNamespace(name=dist))  # type: ignore[arg-type, attr-defined, no-any-return]


def test_a_family_registered_twice_is_refused_naming_both_distributions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two distributions registering one name: one loads, the other is a stale
    install that fails to import. Either way the answer is the duplicate,
    naming who registered it."""
    module = types.ModuleType("_family_pkg_dup")
    module.FAMILY = FakeFamily("dup")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    _patch_entry_points(
        monkeypatch,
        [
            _entry("dup", module.__name__, "pkg-one"),
            # stale: its module is gone, so loading it would fail on its own
            _entry("dup", "_no_such_stale_pkg", "pkg-two"),
        ],
    )
    with pytest.raises(HarnessFamilyError, match="registered twice") as caught:
        families.harness_families()
    assert "'pkg-one'" in str(caught.value)
    assert "'pkg-two'" in str(caught.value)


def test_two_families_with_different_names_are_not_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, {"one": FakeFamily("one"), "two": FakeFamily("two")})
    assert {"one", "two"} <= {f.name for f in families.harness_families()}


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


def _slot() -> slots.Slot:
    return slots.Slot(number=1, database_url="postgresql://x/y", owner="o#1")


_CACHE = {"SANDLOOM_TEST_CACHE_URL": "redis://h/1"}


def test_a_broken_family_is_not_reported_as_redis_being_full(only: Any) -> None:
    class Broken(FakeFamily):
        def slot_bases(
            self,
            urls: Mapping[str, str],
            owner: str,
            environ: MutableMapping[str, str],
        ) -> dict[str, str]:
            raise HarnessFamilyError("the family is broken")

    only(Broken())
    with pytest.raises(HarnessFamilyError, match="the family is broken"):
        slots._point_at(_slot(), dict(_CACHE), {}, True)


@pytest.mark.parametrize("error", [RuntimeError("full"), OSError("full")])
def test_a_full_backend_is_reported_and_the_run_goes_on(
    only: Any, error: Exception, capsys: pytest.CaptureFixture[str]
) -> None:
    class Full(FakeFamily):
        def slot_bases(
            self,
            urls: Mapping[str, str],
            owner: str,
            environ: MutableMapping[str, str],
        ) -> dict[str, str]:
            raise error

    only(Full())
    slot = _slot()
    slots._point_at(slot, dict(_CACHE), {}, True)
    assert "no own Redis indexes (full)" in capsys.readouterr().err
    assert slot.claimed_urls == ()
    assert slot.redis_urls == _CACHE


def test_another_failure_of_a_family_still_raises(only: Any) -> None:
    class Odd(FakeFamily):
        def slot_bases(
            self,
            urls: Mapping[str, str],
            owner: str,
            environ: MutableMapping[str, str],
        ) -> dict[str, str]:
            raise ValueError("not a capacity error")

    only(Odd())
    with pytest.raises(ValueError, match="not a capacity error"):
        slots._point_at(_slot(), dict(_CACHE), {}, True)


# --------------------------------------------------------------------------- #
# The guard against a silent loss of isolation
# --------------------------------------------------------------------------- #


def test_the_pytest_plugin_refuses_a_client_without_its_family(
    only: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped plugin, not only this repository's conftest: a downstream
    suite with the client and no family stops at ``pytest_configure``."""
    only()
    monkeypatch.setattr(plugin, "client_installed", lambda client: True)
    with pytest.raises(HarnessFamilyError, match="no harness family 'redis'"):
        plugin.pytest_configure(None)  # type: ignore[arg-type]


def test_without_a_client_no_family_is_loaded_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse() -> tuple[HarnessFamily, ...]:
        raise AssertionError("discovery ran for a process without a client")

    monkeypatch.setattr(families, "_loaded", None)
    monkeypatch.setattr(families, "_discover", refuse)
    families.require_for_installed({"redis": False})


def test_a_client_without_its_family_is_refused(only: Any) -> None:
    only()
    with pytest.raises(HarnessFamilyError, match="no harness family 'redis'"):
        families.require_for_installed({"redis": True})


def test_a_client_with_its_family_or_without_the_client_is_fine(only: Any) -> None:
    only(FakeFamily("redis"))
    families.require_for_installed({"redis": True})
    only()
    families.require_for_installed({"redis": False})


class _Flush(FakeFamily):
    """Answers ``flush_owned`` as told (``True``: the provider is its own)."""

    def __init__(self, name: str, owns: bool) -> None:
        super().__init__(name)
        self.owns = owns

    async def flush_owned(self, provider: object) -> bool:
        self.calls.append(("flush_owned", provider))
        return self.owns


@pytest.mark.asyncio
async def test_the_cache_flush_stops_at_the_family_that_owns_the_provider(
    only: Any,
) -> None:
    """``sandloom_cache`` flushes through the families: the second owns the
    provider, so the third is never asked."""
    chain = [_Flush("a", False), _Flush("b", True), _Flush("c", True)]
    only(*chain)
    provider = object()
    await flush_owned_cache(provider)
    assert [[call[0] for call in f.calls] for f in chain] == [
        ["flush_owned"],
        ["flush_owned"],
        [],
    ]
    assert chain[1].calls[0][1] is provider


@pytest.mark.asyncio
async def test_the_cache_flush_asks_every_family_when_none_owns_the_provider(
    only: Any,
) -> None:
    chain = [_Flush("a", False), _Flush("b", False)]
    only(*chain)
    await flush_owned_cache(object())  # nobody's provider: nothing flushed, no error
    assert [len(f.calls) for f in chain] == [1, 1]


# --------------------------------------------------------------------------- #
# The seams a family package reaches through sandloom.driverkit.testing_family
# --------------------------------------------------------------------------- #


def test_the_composition_default_names_the_driver_of_an_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        family_seams,
        "root_default_infra",
        lambda: {"cache": {"default": {"driver": "fake-cache"}}, "x": {"y": "bad"}},
    )
    assert family_seams.default_driver("cache", "default") == "fake-cache"
    assert family_seams.default_driver("cache", "other") is None
    assert family_seams.default_driver("eventbus", "default") is None
    assert family_seams.default_driver("x", "y") is None  # not an instance mapping
    monkeypatch.setattr(family_seams, "root_default_infra", lambda: None)
    assert family_seams.default_driver("cache", "default") is None


def test_a_built_in_driver_keeps_the_options_it_was_given() -> None:
    options = {"url": "memory://"}
    moved = family_seams.isolated_instance_options(
        "cache", "default", "memory", options, "gw1"
    )
    assert moved is options


def test_a_driver_an_installed_package_publishes_says_where_the_copy_lives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Moved:
        @classmethod
        def test_isolation(cls, options: Any, worker_id: str) -> dict[str, Any]:
            return {**options, "worker": worker_id}

    monkeypatch.setattr(family_seams, "_driver_class", lambda *_: Moved)
    assert family_seams.isolated_instance_options(
        "cache", "default", "other", {"a": 1}, "gw3"
    ) == {"a": 1, "worker": "gw3"}
