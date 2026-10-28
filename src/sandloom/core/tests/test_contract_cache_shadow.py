"""Shadow hit-rate instrumentation of a potential contract-read cache (ADR-0003).

* OFF by default; a bad configuration disables it (never breaks a call).
* It NEVER alters a result, an error or the order of results — locally and
  over RPC, with data changing between calls (a real cache would have served
  stale answers there; the shadow only counts them).
* What it counts: hit / miss per TTL, ``stale_hit`` when the cached answer
  differs from the fresh one, ``negative`` (never cached), invalidation by
  value (``invalidated_live`` / ``invalidated_expired``), org-wide events,
  reads racing an invalidation are not stored, tenants never share entries,
  whole-cache flushes, the exit report.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException

from sandloom.core import contract_cache_shadow as shadow
from sandloom.core.config.settings import Settings, settings
from sandloom.core.contract_cache_shadow import (
    ShadowContractCache,
    ShadowContractConfig,
)
from sandloom.core.contract_testing import (
    in_process_rpc_client,
    rpc_proxy_for,
    rpc_settings,
)
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import ContractCallError, contract_method

CONTRACT = "shadowdemo.membership.v1"
EVENTS = {
    "shadowdemo.member_role_changed.v1": ("organization_id", "user_id"),
    "shadowdemo.organization_deleted.v1": ("organization_id",),
}
#: This process's RPC identity: the "rpc" path calls the core itself.
_SVC = "svc"


# --------------------------------------------------------------------------- #
# pure semantics (fake clock)
# --------------------------------------------------------------------------- #
class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _cache(clock: Clock, **kw: Any) -> ShadowContractCache:
    cfg = ShadowContractConfig(ttls=(1.0, 5.0), invalidated_by=EVENTS)
    return ShadowContractCache({CONTRACT: cfg}, caller="test", clock=clock, **kw)


def _count(
    cache: ShadowContractCache, ttl: str, outcome: str, method: str = "role"
) -> int:
    return cache.counts.get((CONTRACT, method, ttl, outcome), 0)


def _observe(
    cache: ShadowContractCache,
    org: UUID,
    user: UUID,
    result: Any,
    *,
    tenant: Any = None,
    method: str = "role",
) -> None:
    cache.observe(CONTRACT, method, tenant, (org, user), {}, result, cache.start())


def test_off_by_default() -> None:
    factory: Any = Settings.model_fields["contract_cache_shadow"].default_factory
    assert factory() == {}
    assert shadow.build_from_settings(SimpleNamespace(contract_cache_shadow={})) is None


def test_bad_configuration_disables_the_shadow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "contract_cache_shadow", {CONTRACT: {"ttls": [0]}})
    shadow.reset()
    try:
        assert shadow.current() is None
        assert shadow.for_contract(CONTRACT) is None
    finally:
        shadow.reset()


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"ttls": []},
        {"ttls": [-1]},
        {"ttls": [True]},
        {"invalidated_by": []},
        {"invalidated_by": {"x.v1": "organization_id"}},
    ],
)
def test_config_validation(raw: Any) -> None:
    with pytest.raises(ValueError):
        ShadowContractConfig.parse(CONTRACT, raw)


def test_hit_and_miss_per_ttl() -> None:
    clock, org, user = Clock(), uuid4(), uuid4()
    cache = _cache(clock)
    _observe(cache, org, user, "admin")
    clock.now += 0.5
    _observe(cache, org, user, "admin")
    clock.now += 1.0  # 1.5 s after the first read: TTL 1 expired, TTL 5 not
    _observe(cache, org, user, "admin")
    assert (_count(cache, "1", "miss"), _count(cache, "1", "hit")) == (2, 1)
    assert (_count(cache, "5", "miss"), _count(cache, "5", "hit")) == (1, 2)
    assert _count(cache, "5", "stale_hit") == 0


def test_a_hit_never_refreshes_the_entry() -> None:
    clock, org, user = Clock(), uuid4(), uuid4()
    cache = _cache(clock)
    _observe(cache, org, user, "admin")
    for _ in range(4):
        clock.now += 0.4
        _observe(cache, org, user, "admin")
    # +0.4 / +0.8 hit; +1.2 s after the STORED read the entry is expired
    # (a hit never extends it): miss, re-stored; +1.6 hits the new entry.
    assert (_count(cache, "1", "hit"), _count(cache, "1", "miss")) == (3, 2)


def test_stale_hit_when_the_answer_changed_without_invalidation() -> None:
    clock, org, user = Clock(), uuid4(), uuid4()
    cache = _cache(clock)
    _observe(cache, org, user, "admin")
    _observe(cache, org, user, "member")  # demoted; no event seen
    assert _count(cache, "1", "stale_hit") == 1
    assert _count(cache, "5", "stale_hit") == 1
    _observe(cache, org, user, "member")  # the fresh answer was stored
    assert _count(cache, "1", "stale_hit") == 1


def test_negative_answers_are_never_cached() -> None:
    clock, org, user = Clock(), uuid4(), uuid4()
    cache = _cache(clock)
    negatives: tuple[Any, ...] = (None, None, [], False)
    for result in negatives:
        _observe(cache, org, user, result)
    assert _count(cache, "1", "negative") == 4
    assert _count(cache, "1", "hit") == 0
    # A positive entry that turns negative is a stale hit, then dropped.
    _observe(cache, org, user, "admin")
    _observe(cache, org, user, None)
    _observe(cache, org, user, None)
    assert _count(cache, "1", "stale_hit") == 1
    assert _count(cache, "1", "hit") == 1


def test_invalidation_by_value_drops_only_matching_entries() -> None:
    clock, org, other, user = Clock(), uuid4(), uuid4(), uuid4()
    cache = _cache(clock)
    _observe(cache, org, user, "admin")
    _observe(cache, other, user, "member")
    cache.invalidate(
        "shadowdemo.member_role_changed.v1",
        {"organization_id": str(org), "user_id": str(user), "role": "member"},
    )
    assert _count(cache, "1", "invalidated_live") == 1
    _observe(cache, org, user, "member")  # miss: dropped -> fresh, no stale hit
    _observe(cache, other, user, "member")  # untouched -> hit
    assert _count(cache, "1", "stale_hit") == 0
    assert _count(cache, "1", "hit") == 1


def test_expired_entries_count_as_expired_on_invalidation() -> None:
    clock, org, user = Clock(), uuid4(), uuid4()
    cache = _cache(clock)
    _observe(cache, org, user, "admin")
    clock.now += 2.0
    cache.invalidate(
        "shadowdemo.member_role_changed.v1",
        {"organization_id": str(org), "user_id": str(user)},
    )
    assert _count(cache, "1", "invalidated_expired") == 1
    assert _count(cache, "5", "invalidated_live") == 1


def test_org_wide_event_drops_every_entry_of_the_org() -> None:
    clock, org, other = Clock(), uuid4(), uuid4()
    cache = _cache(clock)
    for user in (uuid4(), uuid4()):
        _observe(cache, org, user, "member")
    _observe(cache, other, uuid4(), "member")
    cache.invalidate(
        "shadowdemo.organization_deleted.v1", {"organization_id": str(org)}
    )
    assert _count(cache, "1", "invalidated_live") == 2


def test_unconfigured_channel_and_fieldless_event() -> None:
    clock, org, user = Clock(), uuid4(), uuid4()
    cache = _cache(clock)
    _observe(cache, org, user, "admin")
    cache.invalidate("billing.credits.spent.v1", {"organization_id": str(org)})
    assert _count(cache, "1", "invalidated_live") == 0
    # A configured event naming none of its fields flushes the contract.
    cache.invalidate("shadowdemo.organization_deleted.v1", {})
    assert _count(cache, "1", "invalidated_live") == 1


def test_a_read_racing_an_invalidation_is_not_stored() -> None:
    clock, org, user = Clock(), uuid4(), uuid4()
    cache = _cache(clock)
    started = cache.start()
    cache.invalidate(
        "shadowdemo.organization_deleted.v1", {"organization_id": str(org)}
    )
    cache.observe(CONTRACT, "role", None, (org, user), {}, "admin", started)
    _observe(cache, org, user, "admin")
    assert _count(cache, "1", "miss") == 2


def test_tenants_and_methods_never_share_entries() -> None:
    clock, org, user = Clock(), uuid4(), uuid4()
    cache = _cache(clock)
    _observe(cache, org, user, "admin", tenant="t1")
    _observe(cache, org, user, "admin", tenant="t2")
    _observe(cache, org, user, "admin", tenant="t1", method="other")
    assert _count(cache, "1", "hit") == 0


def test_unkeyable_arguments_are_not_cached() -> None:
    clock = Clock()
    cache = _cache(clock)
    cache.observe(CONTRACT, "role", None, ([1, 2],), {}, "x", cache.start())
    assert _count(cache, "1", "unkeyable") == 1


def test_flush_and_overflow() -> None:
    clock, org = Clock(), uuid4()
    cache = _cache(clock, max_entries=2)
    for _ in range(3):
        _observe(cache, org, uuid4(), "member")
    assert cache.flushes == {"overflow": 2}  # one per TTL
    cache.flush("subscription_lost")
    assert cache.flushes["subscription_lost"] == 1


def test_exit_report(tmp_path: Path) -> None:
    clock, org, user = Clock(), uuid4(), uuid4()
    cache = _cache(clock)
    _observe(cache, org, user, "admin")
    _observe(cache, org, user, "admin")
    shadow._write_report(cache, str(tmp_path))
    (report,) = tmp_path.glob("shadow-test-*.json")
    data = json.loads(report.read_text())
    assert data["caller"] == "test"
    assert {
        "contract": CONTRACT,
        "method": "role",
        "ttl": "1",
        "outcome": "hit",
        "count": 1,
    } in data["counts"]


def test_committed_events_invalidate_in_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, org, user = Clock(), uuid4(), uuid4()
    cache = _cache(clock)
    _observe(cache, org, user, "admin")
    shadow.configure(cache)
    sent: list[Any] = []

    async def _fake_broadcast(messages: list[dict[str, Any]]) -> None:
        sent.extend(messages)

    monkeypatch.setattr(shadow, "_broadcast", _fake_broadcast)
    event = SimpleNamespace(
        channel="shadowdemo.member_role_changed.v1",
        payload={"organization_id": str(org), "user_id": str(user), "role": "x"},
    )
    unrelated = SimpleNamespace(channel="other.v1", payload={})

    async def _run() -> None:
        shadow.note_committed_events([unrelated, event])
        await asyncio.sleep(0)

    try:
        asyncio.run(_run())
    finally:
        shadow.reset()
    assert _count(cache, "1", "invalidated_live") == 1
    assert sent == [
        {
            "c": "shadowdemo.member_role_changed.v1",
            "p": {"organization_id": str(org), "user_id": str(user)},
        }
    ]


# --------------------------------------------------------------------------- #
# zero semantic change: local and RPC calls with the shadow on vs off
# --------------------------------------------------------------------------- #
class Membership:
    _roles: dict[tuple[UUID, UUID], str] = {}

    @classmethod
    @contract_method
    async def get_role(cls, organization_id: UUID, user_id: UUID) -> str | None:
        return cls._roles.get((organization_id, user_id))

    @classmethod
    @contract_method
    async def orgs(cls, user_id: UUID) -> list[UUID]:
        return sorted(o for (o, u) in cls._roles if u == user_id)

    @classmethod
    @contract_method
    async def denied(cls, organization_id: UUID) -> str:
        raise HTTPException(status_code=403, detail={"org": str(organization_id)})


class _FakeSession:
    def __init__(self) -> None:
        self.info: dict[str, Any] = {}


class _FakeUoW:
    def __init__(self) -> None:
        self.session: _FakeSession | None = None

    async def __aenter__(self) -> _FakeUoW:
        self.session = _FakeSession()
        return self

    async def __aexit__(self, *_: Any) -> None:
        self.session = None


class _FakeDatabase:
    def uow_factory(self) -> Callable[[], _FakeUoW]:
        return _FakeUoW

    def outbox(self) -> None:
        return None

    def audit(self) -> None:
        return None


@pytest.fixture
def core(monkeypatch: pytest.MonkeyPatch) -> Iterator[CoreFacade]:
    for name, value in rpc_settings(_SVC, allowed_calls={_SVC: [CONTRACT]}).items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(shadow, "_cache_provider", lambda: SimpleNamespace())
    facade = CoreFacade(app=FastAPI())
    facade.database = lambda name="default": _FakeDatabase()  # type: ignore[method-assign]
    facade.register_service(CONTRACT, Membership)
    Membership._roles = {}
    yield facade
    shadow.reset()


async def _script(proxy: Any, orgs: list[UUID], users: list[UUID]) -> list[Any]:
    """Reads interleaved with membership changes (no invalidation sent)."""
    out: list[Any] = []
    roles = Membership._roles
    roles[(orgs[0], users[0])] = "admin"
    roles[(orgs[1], users[0])] = "member"
    for step in range(3):
        out.append(await proxy.get_role(orgs[0], users[0]))
        out.append(await proxy.get_role(organization_id=orgs[1], user_id=users[0]))
        out.append(await proxy.orgs(users[0]))
        out.append(await proxy.get_role(orgs[0], users[1]))
        if step == 0:
            roles[(orgs[0], users[0])] = "member"  # demotion
        if step == 1:
            del roles[(orgs[1], users[0])]  # removal
    try:
        await proxy.denied(orgs[0])
    except ContractCallError as exc:
        out.append((exc.status_code, exc.detail))
    out.extend(
        await asyncio.gather(*(proxy.get_role(o, u) for o in orgs for u in users))
    )
    return out


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["local", "rpc"])
async def test_results_errors_and_order_are_identical_on_and_off(
    core: CoreFacade, path: str
) -> None:
    orgs, users = [uuid4(), uuid4()], [uuid4(), uuid4()]
    results: dict[str, list[Any]] = {}
    observer: ShadowContractCache | None = None
    for mode in ("off", "on"):
        if mode == "on":
            observer = ShadowContractCache(
                {
                    CONTRACT: ShadowContractConfig(
                        ttls=(1.0, 5.0), invalidated_by=EVENTS
                    )
                },
                caller="test",
            )
            shadow.configure(observer)
        else:
            shadow.configure(None)
        if path == "local":
            results[mode] = await _script(core.get_service(CONTRACT), orgs, users)
        else:
            async with in_process_rpc_client(core) as client:
                proxy = rpc_proxy_for(core, CONTRACT, client)
                results[mode] = await _script(proxy, orgs, users)
    assert results["on"] == results["off"]
    assert results["on"][1] == "member"  # fresh answers, never cached ones
    assert results["on"][4] == "member" and results["on"][9] is None
    assert observer is not None
    counts = observer.counts
    # The shadow saw what a cache would have done: hits, and stale hits on
    # the demotion / removal it was never told about.
    assert counts.get((CONTRACT, "get_role", "5", "hit"), 0) > 0
    assert counts.get((CONTRACT, "get_role", "5", "stale_hit"), 0) >= 2
    assert counts.get((CONTRACT, "get_role", "5", "negative"), 0) > 0


# --------------------------------------------------------------------------- #
# benefit and safety beyond the hit rate (time avoided, stale decisions,
# per-request share)
# --------------------------------------------------------------------------- #
RANK = {"member": 1, "admin": 2, "owner": 3}


def _ranked(clock: Clock) -> ShadowContractCache:
    cfg = ShadowContractConfig(ttls=(1.0, 5.0), invalidated_by=EVENTS, rank=RANK)
    return ShadowContractCache({CONTRACT: cfg}, caller="test", clock=clock)


def _stale(
    cache: ShadowContractCache, kind: str, ttl: str = "5", m: str = "role"
) -> int:
    return cache.stale.get((CONTRACT, m, ttl, kind), 0)


def test_rank_config_validation() -> None:
    cfg = ShadowContractConfig.parse(CONTRACT, {"rank": RANK})
    assert dict(cfg.rank) == RANK
    bad: dict[str, Any]
    for bad in ({"rank": []}, {"rank": {"admin": "high"}}, {"rank": {"admin": True}}):
        with pytest.raises(ValueError):
            ShadowContractConfig.parse(CONTRACT, bad)


@pytest.mark.parametrize(
    ("first", "second", "kind"),
    [
        ("admin", None, "removed"),  # access revoked since cached
        ("admin", "member", "downgraded"),
        ("member", "owner", "upgraded"),
        ("admin", "auditor", "changed"),  # not on the ladder
    ],
)
def test_stale_hits_are_classified(first: Any, second: Any, kind: str) -> None:
    clock, org, user = Clock(), uuid4(), uuid4()
    cache = _ranked(clock)
    _observe(cache, org, user, first)
    _observe(cache, org, user, second)
    assert _count(cache, "5", "stale_hit") == 1
    assert _stale(cache, kind) == 1
    assert sum(cache.stale.values()) == 2  # one per TTL, same kind
    assert (kind in shadow.OVER_GRANT_KINDS) == (kind in {"removed", "downgraded"})


def test_stale_collection_answers_narrowed_vs_changed() -> None:
    clock, user = Clock(), uuid4()
    a, b, c = uuid4(), uuid4(), uuid4()
    cache = _cache(clock)

    def orgs(result: list[UUID]) -> None:
        cache.observe(CONTRACT, "orgs", None, (user,), {}, result, cache.start())

    orgs([a, b])
    orgs([a])  # removed from b: a cache would still list it
    orgs([a, c])  # added to c (and the [a] entry was replaced)
    assert _stale(cache, "narrowed", m="orgs") == 1
    assert _stale(cache, "changed", m="orgs") == 1
    assert "narrowed" in shadow.OVER_GRANT_KINDS


def test_boolean_answer_turned_false_is_removed() -> None:
    clock, org, user = Clock(), uuid4(), uuid4()
    cache = _cache(clock)
    cache.observe(CONTRACT, "at_least", None, (org, user), {}, True, cache.start())
    cache.observe(CONTRACT, "at_least", None, (org, user), {}, False, cache.start())
    assert _stale(cache, "removed", m="at_least") == 1


def test_saved_time_is_the_measured_duration_of_would_be_hits() -> None:
    clock, org, user = Clock(), uuid4(), uuid4()
    cache = _cache(clock)
    for duration in (0.004, 0.002, 0.003):
        started = cache.start()
        clock.now += duration  # the read takes this long (measured by the clock)
        cache.observe(CONTRACT, "role", None, (org, user), {}, "admin", started)
        clock.now += 0.3
    # TTL 1: miss, then two hits (0.304 / 0.607 s after the stored read).
    assert cache.saved_s[(CONTRACT, "role", "1")] == pytest.approx(0.005)
    assert cache.saved_s[(CONTRACT, "role", "5")] == pytest.approx(0.005)
    reads, seconds = cache.read_s[(CONTRACT, "role")]
    assert reads == 3
    assert seconds == pytest.approx(0.009)
    # An explicit duration wins over the clock.
    cache.observe(
        CONTRACT, "role", None, (org, user), {}, "admin", cache.start(), elapsed=0.5
    )
    assert cache.saved_s[(CONTRACT, "role", "5")] == pytest.approx(0.505)


def _request_app(observer: ShadowContractCache, org: UUID) -> Any:
    app = FastAPI()

    @app.get("/orgs/{n}/role")
    async def role(n: int) -> dict[str, int]:
        for _ in range(n):  # n watched reads of the same key
            started = observer.start()
            observer.observe(
                CONTRACT, "role", None, (org, n), {}, "admin", started, elapsed=0.001
            )
        return {"n": n}

    @app.get("/plain")
    async def plain() -> dict[str, str]:
        return {}

    @app.get("/health/x")
    async def probe() -> dict[str, str]:
        started = observer.start()
        observer.observe(CONTRACT, "role", None, (org, 99), {}, "x", started)
        return {}

    app.add_middleware(shadow.ShadowRequestMiddleware)
    return app


@pytest.mark.asyncio
async def test_reads_are_attributed_to_http_requests() -> None:
    import httpx

    clock, org = Clock(), uuid4()
    observer = _cache(clock)
    shadow.configure(observer)
    try:
        app = _request_app(observer, org)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            for path in ("/orgs/3/role", "/orgs/1/role", "/plain", "/plain"):
                assert (await client.get(path)).status_code == 200
            assert (await client.get("/health/x")).status_code == 200  # excluded
    finally:
        shadow.reset()
    totals = observer.request_totals()
    assert (totals["requests"], totals["with_reads"], totals["reads"]) == (4, 2, 4)
    # /orgs/3: miss + 2 hits; /orgs/1: a miss on another key (n is an argument).
    assert totals["by_ttl"]["1"] == {
        "hits": 2,
        "requests_with_hit": 1,
        "saved_s": pytest.approx(0.002),
        "stale": 0,
    }
    routes = observer.routes
    assert routes["GET /orgs/{n}/role"]["requests"] == 2
    assert routes["GET /plain"] == {
        "requests": 2,
        "with_reads": 0,
        "reads": 0,
        "by_ttl": {},
    }
    assert not any(r.startswith("GET /health") for r in routes)
    # The excluded probe's read still counts per call, never per request.
    assert _count(observer, "1", "miss") == 3
    report = observer.snapshot()["requests"]
    assert report["requests"] == 4 and "GET /orgs/{n}/role" in report["routes"]


@pytest.mark.asyncio
async def test_a_read_after_the_request_finished_is_not_attributed() -> None:
    clock, org = Clock(), uuid4()
    observer = _cache(clock)
    tally = shadow._RequestTally()
    token = shadow._REQUEST.set(tally)
    try:
        _observe(observer, org, uuid4(), "admin")
        tally.open = False  # e.g. a post-commit task that copied the context
        _observe(observer, org, uuid4(), "admin")
    finally:
        shadow._REQUEST.reset(token)
    assert tally.reads == 1


def test_middleware_is_not_installed_when_the_shadow_is_off() -> None:
    from sandloom.main import app

    assert shadow.current() is None
    middleware: Any = shadow.ShadowRequestMiddleware
    assert not any(m.cls is middleware for m in app.user_middleware)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["local", "rpc"])
async def test_real_contract_reads_feed_the_request_and_stale_counters(
    core: CoreFacade, path: str
) -> None:
    observer = ShadowContractCache(
        {CONTRACT: ShadowContractConfig(ttls=(5.0,), invalidated_by=EVENTS, rank=RANK)},
        caller="test",
    )
    org, user = uuid4(), uuid4()
    Membership._roles = {(org, user): "admin"}

    async def reads(proxy: Any) -> None:
        shadow.configure(observer)
        tally_token = shadow._REQUEST.set(tally)
        try:
            assert await proxy.get_role(org, user) == "admin"
            Membership._roles[(org, user)] = "member"  # demoted, no event
            assert await proxy.get_role(org, user) == "member"
        finally:
            shadow._REQUEST.reset(tally_token)

    tally = shadow._RequestTally()
    if path == "local":
        await reads(core.get_service(CONTRACT))
    else:
        async with in_process_rpc_client(core) as client:
            proxy = rpc_proxy_for(core, CONTRACT, client)
            # A method the client has not described yet may write, so its
            # first call is never measured: describe it first (shadow off),
            # as any earlier call in a long-running process has.
            assert await proxy.get_role(uuid4(), user) is None
            await reads(proxy)
    assert tally.reads == 2 and tally.hits == {5.0: 1} and tally.stale == {5.0: 1}
    assert observer.stale == {(CONTRACT, "get_role", "5", "downgraded"): 1}
    assert observer.saved_s[(CONTRACT, "get_role", "5")] > 0


# --------------------------------------------------------------------------- #
# OFF is the default and does no work
# --------------------------------------------------------------------------- #
def test_the_shipped_default_is_off() -> None:
    assert settings.contract_cache_shadow == {}
    shadow.reset()
    try:
        assert shadow.current() is None
        assert shadow.for_contract("tenancy.membership.v1") is None
    finally:
        shadow.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["local", "rpc"])
async def test_off_path_never_reaches_the_bookkeeping(
    core: CoreFacade, path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the shadow off a read pays one ``for_contract`` check, nothing else:
    no clock stamp, no observe, no request tally, no invalidation work."""

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("shadow bookkeeping ran while OFF")

    shadow.configure(None)
    for name in ("observe_read", "_ensure_listener", "note_committed_events"):
        monkeypatch.setattr(shadow, name, _boom)
    monkeypatch.setattr(ShadowContractCache, "start", _boom)
    monkeypatch.setattr(ShadowContractCache, "observe", _boom)
    checks: list[str] = []
    real_for_contract = shadow.for_contract

    def _counting(contract: str) -> Any:
        checks.append(contract)
        return real_for_contract(contract)

    monkeypatch.setattr(shadow, "for_contract", _counting)
    org, user = uuid4(), uuid4()
    Membership._roles = {(org, user): "admin"}
    if path == "local":
        proxy: Any = core.get_service(CONTRACT)
        assert await proxy.get_role(org, user) == "admin"
    else:
        async with in_process_rpc_client(core) as client:
            proxy = rpc_proxy_for(core, CONTRACT, client)
            # describe the method first: an undescribed one may write, and a
            # write is never measured (no check at all)
            assert await proxy.get_role(uuid4(), user) is None
            checks.clear()
            assert await proxy.get_role(org, user) == "admin"
    assert checks == [CONTRACT]  # exactly one constant-time check per read
    assert shadow._REQUEST.get() is None
