"""Shadow hit-rate instrumentation for a POTENTIAL caller-side contract-read cache.

ADR-0003 ("Then"): before deciding whether a cross-request cache of
membership reads (``tenancy.membership.v1``) is worth having at all, measure
how often it WOULD hit on the real mix. This module never serves anything
from a cache: every read still goes to the provider exactly as before, the
result handed to the caller is the provider's, and nothing the caller sees
changes (order, values, errors, timing apart from the bookkeeping below). It
only records, per configured contract, what a cache with TTL 1 s / 5 s / …
keyed by (caller service, contract, method, bound tenant, arguments) and
invalidated by the provider's events WOULD have done.

OFF by default. Enable per contract with ``CONTRACT_CACHE_SHADOW`` (JSON)::
    {"tenancy.membership.v1": {
        "ttls": [1, 5],
        "rank": {"member": 1, "admin": 2, "owner": 3},
        "invalidated_by": {
            "tenancy.member_added.v1": ["organization_id", "user_id"],
            "tenancy.member_removed.v1": ["organization_id", "user_id"],
            "tenancy.member_role_changed.v1": ["organization_id", "user_id"],
            "tenancy.organization_deleted.v1": ["organization_id"]}}}

Set the same value on EVERY process (caller and provider): the provider's
process announces the invalidations, the callers apply them.

Outcomes, per (caller, contract, method, ttl) — Prometheus counter
``sandloom_contract_cache_shadow_total{outcome=...}``:

* ``hit`` — an entry younger than the TTL existed: the call would have been
  answered locally (the saving);
* ``miss`` — no live entry: the call would have gone to the provider;
* ``stale_hit`` — a ``hit`` whose cached answer DIFFERS from the provider's
  fresh one: the cache would have served a stale answer (an invalidation that
  did not arrive, or a change the declared events do not cover);
* ``negative`` — the answer was negative (``None`` / ``False`` / empty):
  never cached (ADR-0003: no negative caching), counted apart;
* ``unkeyable`` — arguments or result not digestible: never cached;
* ``invalidated_live`` — an invalidation dropped an entry still younger than
  the TTL: without that invalidation the cache would have served a stale
  answer (would-be-stale-on-invalidation);
* ``invalidated_expired`` — an invalidation found the entry already expired.

Beyond the per-call rate (a high hit rate on a rarely called method is a small
saving), three more things are recorded so a run can judge benefit AND safety:

* **per HTTP request** (:class:`ShadowRequestMiddleware`, installed by
  ``create_app`` only while the shadow is on; kernel ops paths excluded):
  requests, requests making at least one watched read, watched reads, and per
  TTL the would-be hits, requests with a hit and the time those hits would
  have saved — ``sandloom_contract_cache_shadow_requests_total{with_reads}``,
  ``..._request_reads_total``, ``..._request_hits_total{ttl}``,
  ``..._requests_with_hit_total{ttl}``,
  ``..._request_saved_seconds_total{ttl}``; per route in the JSON report;
* **time avoided**: the measured duration (wall clock, read start to decoded
  result) of every read that would have been a hit,
  ``sandloom_contract_cache_shadow_saved_seconds_total{contract,method,ttl}``, and
  of every observed read (``..._read_seconds_total{contract,method}``) for
  the mean read cost;
* **potentially stale decisions**: every ``stale_hit`` (the cached answer
  differs from the live one the shadow just read) by kind —
  ``sandloom_contract_cache_shadow_stale_total{contract,method,ttl,kind}``:
  ``removed`` (cached positive, live negative: access since revoked),
  ``downgraded`` / ``upgraded`` (both on the contract's optional ``rank``
  ladder, e.g. ``{"member": 1, "admin": 2, "owner": 3}``), ``narrowed`` (the
  live collection is a strict subset of the cached one), ``changed`` (other).
  ``removed`` / ``downgraded`` / ``narrowed`` would have OVER-granted.

Invalidations are matched by VALUE: an event whose configured fields carry
``{org, user}`` drops every entry of the contract whose call arguments
include all of those values (conservative: over-invalidates, never under-).
An event naming none of its fields flushes the whole contract. In the
publishing process the drop runs at once after commit; other processes get it
over Redis pub/sub (:data:`INVALIDATION_CHANNEL`) and drop EVERYTHING when
their subscription is (re-)established or lost — the rules ADR-0003 decided
for the real cache, so the shadow also measures their cost.

Entries are stamped with the time the read STARTED and are not stored when an
invalidation arrived while the read was in flight. Memory is bounded
(``CONTRACT_CACHE_SHADOW_MAX_ENTRIES`` per TTL; cleared wholesale above it).
``CONTRACT_CACHE_SHADOW_REPORT_DIR`` writes a JSON snapshot of the counters
at process exit (``shadow-<caller>-<pid>.json``) for benchmark runs.

Every hook is exception-safe: a failure in the bookkeeping is logged at debug
level and never reaches the call.
"""

from __future__ import annotations

import asyncio
import atexit
import contextvars
import json
import logging
import os
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from prometheus_client import Counter

import sandloom.core.infra.access as _access
import sandloom.core.tenant_context as _tenant_context
from sandloom.core import contract_wire
from sandloom.core.config.settings import settings
from sandloom.core.unit_of_work import dispatch as _unit_of_work_dispatch

logger = logging.getLogger("sandloom.core.contract_cache_shadow")

#: Redis pub/sub channel carrying invalidations between processes.
INVALIDATION_CHANNEL = "sandloom:contract-cache-shadow:invalidations"

OUTCOMES = (
    "hit",
    "miss",
    "stale_hit",
    "negative",
    "unkeyable",
    "invalidated_live",
    "invalidated_expired",
)

_METRIC = Counter(
    "sandloom_contract_cache_shadow_total",
    "Shadow (never served) contract-read cache: what a caller-side TTL cache "
    "would have done per call / invalidation (ADR-0003)",
    ["caller", "contract", "method", "ttl", "outcome"],
)
_METRIC_FLUSHES = Counter(
    "sandloom_contract_cache_shadow_flushes_total",
    "Whole-cache drops of the shadow contract-read cache",
    ["caller", "reason"],
)

_STALE_KINDS = ("removed", "downgraded", "upgraded", "narrowed", "changed")
#: Kinds of stale hit that would have granted MORE than the live answer.
OVER_GRANT_KINDS = frozenset({"removed", "downgraded", "narrowed"})

_METRIC_STALE = Counter(
    "sandloom_contract_cache_shadow_stale_total",
    "Shadow stale hits (a decision a cache would have made on an answer that "
    "differs from the live one), by kind",
    ["caller", "contract", "method", "ttl", "kind"],
)
_METRIC_SAVED = Counter(
    "sandloom_contract_cache_shadow_saved_seconds_total",
    "Measured duration of the reads a shadow cache hit would have avoided",
    ["caller", "contract", "method", "ttl"],
)
_METRIC_READ_SECONDS = Counter(
    "sandloom_contract_cache_shadow_read_seconds_total",
    "Measured duration of every read observed by the shadow",
    ["caller", "contract", "method"],
)
_METRIC_REQUESTS = Counter(
    "sandloom_contract_cache_shadow_requests_total",
    "HTTP requests seen while the shadow is on, by whether they made at least "
    "one watched contract read",
    ["caller", "with_reads"],
)
_METRIC_REQUEST_READS = Counter(
    "sandloom_contract_cache_shadow_request_reads_total",
    "Watched contract reads made by HTTP requests",
    ["caller"],
)
_METRIC_REQUEST_HITS = Counter(
    "sandloom_contract_cache_shadow_request_hits_total",
    "Would-be cache hits inside HTTP requests",
    ["caller", "ttl"],
)
_METRIC_REQUESTS_WITH_HIT = Counter(
    "sandloom_contract_cache_shadow_requests_with_hit_total",
    "HTTP requests with at least one would-be cache hit",
    ["caller", "ttl"],
)
_METRIC_REQUEST_SAVED = Counter(
    "sandloom_contract_cache_shadow_request_saved_seconds_total",
    "Read time the would-be hits inside HTTP requests would have saved",
    ["caller", "ttl"],
)

_SCALARS = (str, int, float, bool, UUID)
_NO_VALUE: Any = object()
_MAX_KEPT_ITEMS = 256


@dataclass(frozen=True)
class ShadowContractConfig:
    """What the shadow simulates for one contract."""

    ttls: tuple[float, ...] = (1.0, 5.0)
    #: event channel -> payload fields naming the invalidated entries.
    invalidated_by: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: optional ladder of scalar answers (e.g. roles) to tell a downgrade
    #: from an upgrade in a stale hit.
    rank: Mapping[str, int] = field(default_factory=dict)

    @classmethod
    def parse(cls, contract: str, raw: Any) -> ShadowContractConfig:
        if not isinstance(raw, Mapping):
            raise ValueError(f"CONTRACT_CACHE_SHADOW[{contract!r}] must be an object")
        ttls_raw = raw.get("ttls", (1, 5))
        if (
            not isinstance(ttls_raw, (list, tuple))
            or not ttls_raw
            or any(
                isinstance(t, bool) or not isinstance(t, (int, float)) or t <= 0
                for t in ttls_raw
            )
        ):
            raise ValueError(
                f"CONTRACT_CACHE_SHADOW[{contract!r}].ttls must be positive numbers"
            )
        events_raw = raw.get("invalidated_by", {})
        if not isinstance(events_raw, Mapping):
            raise ValueError(
                f"CONTRACT_CACHE_SHADOW[{contract!r}].invalidated_by must be an object"
            )
        events: dict[str, tuple[str, ...]] = {}
        for channel, fields in events_raw.items():
            if not isinstance(fields, (list, tuple)) or not all(
                isinstance(f, str) for f in fields
            ):
                raise ValueError(
                    f"CONTRACT_CACHE_SHADOW[{contract!r}].invalidated_by"
                    f"[{channel!r}] must be a list of payload field names"
                )
            events[str(channel)] = tuple(fields)
        rank_raw = raw.get("rank", {})
        if not isinstance(rank_raw, Mapping) or not all(
            isinstance(v, int) and not isinstance(v, bool) for v in rank_raw.values()
        ):
            raise ValueError(
                f"CONTRACT_CACHE_SHADOW[{contract!r}].rank must map answers to integers"
            )
        return cls(
            ttls=tuple(sorted({float(t) for t in ttls_raw})),
            invalidated_by=events,
            rank={str(k): int(v) for k, v in rank_raw.items()},
        )


@dataclass
class _Entry:
    stored_at: float
    digest: str
    method: str
    values: frozenset[str]
    #: the cached answer in comparable form (scalar / frozenset), or _NO_VALUE.
    value: Any = _NO_VALUE


def _comparable(result: Any) -> Any:
    """A small, comparable copy of an answer (to classify a stale hit)."""
    if result is None or isinstance(result, _SCALARS):
        return result
    if (
        isinstance(result, (list, tuple, set, frozenset))
        and (len(result) <= _MAX_KEPT_ITEMS)
        and all(isinstance(v, _SCALARS) for v in result)
    ):
        return frozenset(str(v) for v in result)
    return _NO_VALUE


def _stale_kind(cached: Any, live: Any, negative: bool, rank: Mapping[str, int]) -> str:
    if negative:
        return "removed"
    if cached is _NO_VALUE or live is _NO_VALUE:
        return "changed"
    if isinstance(cached, str) and isinstance(live, str):
        if cached in rank and live in rank and rank[cached] != rank[live]:
            return "downgraded" if rank[live] < rank[cached] else "upgraded"
        return "changed"
    if isinstance(cached, frozenset) and isinstance(live, frozenset):
        return "narrowed" if live < cached else "changed"
    return "changed"


class _RequestTally:
    """What one HTTP request's watched reads would have got from a cache."""

    __slots__ = ("hits", "open", "reads", "saved", "stale")

    def __init__(self) -> None:
        self.open = True
        self.reads = 0
        self.hits: dict[float, int] = {}
        self.saved: dict[float, float] = {}
        self.stale: dict[float, int] = {}


_REQUEST: contextvars.ContextVar[_RequestTally | None] = contextvars.ContextVar(
    "sandloom_contract_cache_shadow_request", default=None
)


def _new_route_stats() -> dict[str, Any]:
    return {"requests": 0, "with_reads": 0, "reads": 0, "by_ttl": {}}


def _merge_route_stats(into: dict[str, Any], other: Mapping[str, Any]) -> None:
    for key in ("requests", "with_reads", "reads"):
        into[key] += int(other[key])
    for ttl, row in other["by_ttl"].items():
        dst = into["by_ttl"].setdefault(
            ttl, {"hits": 0, "requests_with_hit": 0, "saved_s": 0.0, "stale": 0}
        )
        for k in ("hits", "requests_with_hit", "stale"):
            dst[k] += int(row[k])
        dst["saved_s"] += float(row["saved_s"])


def _values(args: Iterable[Any], kwargs: Mapping[str, Any]) -> frozenset[str] | None:
    out: set[str] = set()
    for value in (*args, *kwargs.values()):
        if value is None:
            continue
        if not isinstance(value, _SCALARS):
            return None
        out.add(str(value))
    return frozenset(out)


def _args_key(args: Iterable[Any], kwargs: Mapping[str, Any]) -> Any:
    key = (
        tuple((type(v).__name__, str(v)) for v in args),
        tuple(sorted((k, type(v).__name__, str(v)) for k, v in kwargs.items())),
    )
    return key


def _is_negative(result: Any) -> bool:
    if result is None or result is False:
        return True
    return isinstance(result, (list, tuple, set, frozenset, dict, str)) and not result


def _ttl_label(ttl: float) -> str:
    return f"{ttl:g}"


class ShadowContractCache:
    """The simulated cache (one per process). Never answers a call."""

    def __init__(
        self,
        contracts: Mapping[str, ShadowContractConfig],
        *,
        caller: str = "app",
        max_entries: int = 100_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.contracts = dict(contracts)
        self.caller = caller
        self.max_entries = max_entries
        self._clock = clock
        self._ttls = sorted({t for c in self.contracts.values() for t in c.ttls})
        #: ttl -> key -> entry; key = (contract, method, tenant, args key).
        self._entries: dict[float, dict[Any, _Entry]] = {t: {} for t in self._ttls}
        #: channel -> [(contract, fields)].
        self._routes: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
        for contract, cfg in self.contracts.items():
            for channel, fields in cfg.invalidated_by.items():
                self._routes.setdefault(channel, []).append((contract, fields))
        self.generation = 0
        #: (contract, method, ttl label, outcome) -> count (the JSON report).
        self.counts: dict[tuple[str, str, str, str], int] = {}
        self.flushes: dict[str, int] = {}
        self._children: dict[tuple[int, tuple[str, ...]], Any] = {}
        #: (contract, method, ttl label, kind) -> stale hits.
        self.stale: dict[tuple[str, str, str, str], int] = {}
        #: (contract, method, ttl label) -> seconds the would-be hits took.
        self.saved_s: dict[tuple[str, str, str], float] = {}
        #: (contract, method) -> (reads, seconds) of every observed read.
        self.read_s: dict[tuple[str, str], tuple[int, float]] = {}
        #: "METHOD /route/{template}" -> per-request stats (the JSON report).
        self.routes: dict[str, dict[str, Any]] = {}

    # -- bookkeeping ------------------------------------------------------
    def _child(self, metric: Any, *labels: str) -> Any:
        """``metric.labels(caller, *labels)``, memoised (a hot path when ON)."""
        key = (id(metric), labels)
        child = self._children.get(key)
        if child is None:
            child = self._children[key] = metric.labels(self.caller, *labels)
        return child

    def _count(self, contract: str, method: str, ttl: float, outcome: str) -> None:
        label = _ttl_label(ttl)
        key = (contract, method, label, outcome)
        self.counts[key] = self.counts.get(key, 0) + 1
        self._child(_METRIC, contract, method, label, outcome).inc()

    def watches(self, contract: str) -> bool:
        return contract in self.contracts

    def invalidation_channels(self) -> frozenset[str]:
        return frozenset(self._routes)

    def start(self) -> tuple[float, int]:
        """Stamp a read as it starts: (time, invalidation generation)."""
        return self._clock(), self.generation

    # -- calls ------------------------------------------------------------
    def observe(  # noqa: C901
        self,
        contract: str,
        method: str,
        tenant: Any,
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any],
        result: Any,
        started: tuple[float, int],
        *,
        elapsed: float | None = None,
    ) -> None:
        """Record what the cache would have done for one completed read.

        ``elapsed`` is the read's measured duration (default: clock now minus
        ``started``) — what a hit would have saved.
        """
        cfg = self.contracts.get(contract)
        if cfg is None:
            return
        started_at, generation = started
        duration = max(0.0, self._clock() - started_at) if elapsed is None else elapsed
        reads, seconds = self.read_s.get((contract, method), (0, 0.0))
        self.read_s[(contract, method)] = (reads + 1, seconds + duration)
        self._child(_METRIC_READ_SECONDS, contract, method).inc(duration)
        tally = _REQUEST.get()
        if tally is not None and not tally.open:
            tally = None
        if tally is not None:
            tally.reads += 1
        values = _values(args, kwargs)
        digest: str | None = None
        if values is not None:
            try:
                digest = contract_wire.digest(result)
            except Exception:  # noqa: BLE001 - not digestible: not cacheable
                digest = None
        if values is None or digest is None:
            for ttl in cfg.ttls:
                self._count(contract, method, ttl, "unkeyable")
            return
        tenant_key = None if tenant is None else str(tenant)
        key = (contract, method, tenant_key, _args_key(args, kwargs))
        negative = _is_negative(result)
        live_value = _NO_VALUE if negative else _comparable(result)
        for ttl in cfg.ttls:
            entries = self._entries[ttl]
            entry = entries.get(key)
            # The lookup a real cache does BEFORE calling the provider.
            if entry is not None and started_at - entry.stored_at < ttl:
                self._count(contract, method, ttl, "hit")
                self._saved(contract, method, ttl, duration, tally)
                if entry.digest != digest:
                    self._count(contract, method, ttl, "stale_hit")
                    kind = _stale_kind(entry.value, live_value, negative, cfg.rank)
                    self._stale(contract, method, ttl, kind, tally)
                    entries.pop(key, None)
                else:
                    continue  # served from cache: nothing new stored
            else:
                if entry is not None:
                    entries.pop(key, None)
                self._count(contract, method, ttl, "negative" if negative else "miss")
            if negative or generation != self.generation:
                continue  # never cached / raced an invalidation
            if len(entries) >= self.max_entries:
                entries.clear()
                self._flushed("overflow")
            entries[key] = _Entry(started_at, digest, method, values, live_value)

    def _saved(
        self,
        contract: str,
        method: str,
        ttl: float,
        seconds: float,
        tally: _RequestTally | None,
    ) -> None:
        label = _ttl_label(ttl)
        key = (contract, method, label)
        self.saved_s[key] = self.saved_s.get(key, 0.0) + seconds
        self._child(_METRIC_SAVED, contract, method, label).inc(seconds)
        if tally is not None:
            tally.hits[ttl] = tally.hits.get(ttl, 0) + 1
            tally.saved[ttl] = tally.saved.get(ttl, 0.0) + seconds

    def _stale(
        self,
        contract: str,
        method: str,
        ttl: float,
        kind: str,
        tally: _RequestTally | None,
    ) -> None:
        label = _ttl_label(ttl)
        key = (contract, method, label, kind)
        self.stale[key] = self.stale.get(key, 0) + 1
        self._child(_METRIC_STALE, contract, method, label, kind).inc()
        if tally is not None:
            tally.stale[ttl] = tally.stale.get(ttl, 0) + 1

    # -- requests ---------------------------------------------------------
    def record_request(self, tally: _RequestTally, route: str) -> None:
        """Fold one finished HTTP request's tally into the counters."""
        stats = self.routes.get(route)
        if stats is None:
            stats = self.routes[route] = _new_route_stats()
        stats["requests"] += 1
        with_reads = tally.reads > 0
        self._child(_METRIC_REQUESTS, "true" if with_reads else "false").inc()
        if not with_reads:
            return
        stats["with_reads"] += 1
        stats["reads"] += tally.reads
        self._child(_METRIC_REQUEST_READS).inc(tally.reads)
        for ttl in self._ttls:
            label = _ttl_label(ttl)
            row = stats["by_ttl"].setdefault(
                label, {"hits": 0, "requests_with_hit": 0, "saved_s": 0.0, "stale": 0}
            )
            hits = tally.hits.get(ttl, 0)
            if not hits:
                continue
            saved = tally.saved.get(ttl, 0.0)
            row["hits"] += hits
            row["requests_with_hit"] += 1
            row["saved_s"] += saved
            row["stale"] += tally.stale.get(ttl, 0)
            self._child(_METRIC_REQUEST_HITS, label).inc(hits)
            self._child(_METRIC_REQUESTS_WITH_HIT, label).inc()
            self._child(_METRIC_REQUEST_SAVED, label).inc(saved)

    def request_totals(self) -> dict[str, Any]:
        """Per-request stats summed over every route."""
        total = _new_route_stats()
        for stats in self.routes.values():
            _merge_route_stats(total, stats)
        return total

    # -- invalidation -----------------------------------------------------
    def invalidate(self, channel: str, payload: Mapping[str, Any]) -> None:
        """Apply one committed event of ``channel`` (no-op if not configured)."""
        routes = self._routes.get(channel)
        if not routes:
            return
        self.generation += 1
        now = self._clock()
        for contract, fields in routes:
            match = {str(payload[f]) for f in fields if payload.get(f) is not None}
            ttls = self.contracts[contract].ttls
            for ttl in ttls:
                entries = self._entries[ttl]
                for key in [k for k in entries if k[0] == contract]:
                    entry = entries[key]
                    if match and not match <= entry.values:
                        continue
                    live = now - entry.stored_at < ttl
                    outcome = "invalidated_live" if live else "invalidated_expired"
                    self._count(contract, entry.method, ttl, outcome)
                    del entries[key]

    def flush(self, reason: str) -> None:
        """Drop everything (subscription lost / re-established)."""
        self.generation += 1
        for entries in self._entries.values():
            entries.clear()
        self._flushed(reason)

    def _flushed(self, reason: str) -> None:
        self.flushes[reason] = self.flushes.get(reason, 0) + 1
        self._child(_METRIC_FLUSHES, reason).inc()

    def snapshot(self) -> dict[str, Any]:
        """Counters as plain JSON (the exit report / diagnostics)."""
        rows = [
            {"contract": c, "method": m, "ttl": t, "outcome": o, "count": n}
            for (c, m, t, o), n in sorted(self.counts.items())
        ]
        return {
            "caller": self.caller,
            "pid": os.getpid(),
            "contracts": {
                name: {
                    "ttls": list(cfg.ttls),
                    "invalidated_by": {
                        k: list(v) for k, v in cfg.invalidated_by.items()
                    },
                    "rank": dict(cfg.rank),
                }
                for name, cfg in self.contracts.items()
            },
            "counts": rows,
            "flushes": dict(self.flushes),
            "stale": [
                {"contract": c, "method": m, "ttl": t, "kind": k, "count": n}
                for (c, m, t, k), n in sorted(self.stale.items())
            ],
            "saved_s": [
                {"contract": c, "method": m, "ttl": t, "seconds": round(v, 6)}
                for (c, m, t), v in sorted(self.saved_s.items())
            ],
            "read_s": [
                {"contract": c, "method": m, "reads": n, "seconds": round(v, 6)}
                for (c, m), (n, v) in sorted(self.read_s.items())
            ],
            "requests": {**self.request_totals(), "routes": self.routes},
        }


# --------------------------------------------------------------------------- #
# Process-wide observer (built lazily from settings; None = OFF)
# --------------------------------------------------------------------------- #
_UNSET: Any = object()
_observer: Any = _UNSET
_Listener = tuple[asyncio.Task[None], asyncio.AbstractEventLoop, asyncio.Event]
_listener: _Listener | None = None


def build_from_settings(settings: Any) -> ShadowContractCache | None:
    raw = getattr(settings, "contract_cache_shadow", None) or {}
    if not raw:
        return None
    contracts = {
        str(name): ShadowContractConfig.parse(str(name), cfg)
        for name, cfg in raw.items()
    }
    caller = (
        getattr(settings, "rpc_service_id", None)
        or getattr(settings, "sandloom_service_name", None)
        or "app"
    )
    return ShadowContractCache(
        contracts,
        caller=str(caller),
        max_entries=int(
            getattr(settings, "contract_cache_shadow_max_entries", 100_000)
        ),
    )


def _report_dir(settings: Any) -> str | None:
    value = getattr(settings, "contract_cache_shadow_report_dir", None)
    return str(value) if value else None


def _write_report(observer: ShadowContractCache, directory: str) -> None:
    try:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"shadow-{observer.caller}-{os.getpid()}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(observer.snapshot(), fh, indent=1, sort_keys=True)
    except Exception:  # a report must never fail the exit
        logger.debug("contract cache shadow report failed", exc_info=True)


def current() -> ShadowContractCache | None:
    """The process's shadow observer, or ``None`` when OFF (the default)."""
    global _observer
    observer = _observer
    if observer is _UNSET:
        try:
            observer = build_from_settings(settings)
            directory = _report_dir(settings) if observer is not None else None
        except Exception:  # a bad config disables the shadow
            logger.warning("CONTRACT_CACHE_SHADOW ignored", exc_info=True)
            observer, directory = None, None
        _observer = observer
        if observer is not None:
            logger.info(
                "contract cache SHADOW on for %s (never serves from cache)",
                sorted(observer.contracts),
            )
            if directory:
                atexit.register(_write_report, observer, directory)
    return observer  # type: ignore[no-any-return]


def configure(observer: ShadowContractCache | None) -> None:
    """Install ``observer`` (tests) — ``None`` turns the shadow OFF."""
    global _observer
    _observer = observer
    _arm_commit_hook()


def reset() -> None:
    """Forget the observer: the next :func:`current` re-reads the settings."""
    global _observer
    _observer = _UNSET
    _arm_commit_hook()


def _on_committed_events(events: list[Any]) -> None:
    """The Unit of Work's committed-events hook (see :func:`_arm_commit_hook`)."""
    if current() is None:
        # OFF (the default) until configure() / reset(): stop the commit
        # pipeline from calling in at all — the decision is cached like
        # current()'s, so this changes nothing but the cost.
        _unit_of_work_dispatch.set_committed_events_hook(None)
        return
    note_committed_events(events)


def _arm_commit_hook() -> None:
    """Tap the commit pipeline: committed events feed :func:`note_committed_events`.

    The Unit of Work calls the hook after every commit with surviving outbox
    events; the first call settles :func:`current` (built from settings) and
    disarms the hook when the shadow is OFF.
    """
    _unit_of_work_dispatch.set_committed_events_hook(_on_committed_events)


def for_contract(contract: str) -> ShadowContractCache | None:
    """The observer if it watches ``contract`` (the hot-path check)."""
    observer = current()
    if observer is None or contract not in observer.contracts:
        return None
    return observer


#: Kernel ops paths: not application requests (probes, scrapes, internal RPC).
_EXCLUDED_PREFIXES = (
    "/_sandloom/",
    "/health",
    "/ready",
    "/metrics",
    "/diagnostics/",
    "/contracts/",
)


def _route_label(scope: Mapping[str, Any]) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    method = str(scope.get("method", "?"))
    return f"{method} {path}" if path else f"{method} <unrouted>"


class ShadowRequestMiddleware:
    """Attributes watched reads to the HTTP request that made them.

    Installed by ``create_app`` ONLY when the shadow is on (zero cost when
    off). Pure ASGI; never alters the request or the response, and a
    bookkeeping failure never reaches them.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or str(scope.get("path", "")).startswith(
            _EXCLUDED_PREFIXES
        ):
            await self.app(scope, receive, send)
            return
        observer = current()
        if observer is None:
            await self.app(scope, receive, send)
            return
        tally = _RequestTally()
        token = _REQUEST.set(tally)
        try:
            await self.app(scope, receive, send)
        finally:
            _REQUEST.reset(token)
            tally.open = False
            try:
                observer.record_request(tally, _route_label(scope))
            except Exception:  # bookkeeping never fails a request
                logger.debug("contract cache shadow request failed", exc_info=True)


def observe_read(
    observer: ShadowContractCache,
    contract: str,
    method: str,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
    result: Any,
    started: tuple[float, int],
) -> None:
    """Exception-safe :meth:`ShadowContractCache.observe` (+ listener start)."""
    try:
        observer.observe(
            contract,
            method,
            _tenant_context.current_tenant_id_optional(),
            args,
            kwargs,
            result,
            started,
        )
        _ensure_listener(observer)
    except Exception:  # bookkeeping never reaches the call
        logger.debug("contract cache shadow observe failed", exc_info=True)


def note_committed_events(events: Iterable[Any]) -> None:
    """Post-commit hook: apply + broadcast the configured invalidations.

    ``events`` are the committed outbox events (``.channel`` / ``.payload``).
    """
    observer = _observer
    if observer is _UNSET or observer is None:
        observer = current()
        if observer is None:
            return
    try:
        channels = observer.invalidation_channels()
        announce: list[dict[str, Any]] = []
        for event in events:
            channel = getattr(event, "channel", None)
            if channel not in channels:
                continue
            payload = getattr(event, "payload", None) or {}
            observer.invalidate(channel, payload)
            fields = {f for _c, fs in observer._routes[channel] for f in fs}
            keys = {f: str(payload[f]) for f in fields if payload.get(f) is not None}
            announce.append({"c": channel, "p": keys})
        if announce:
            loop = asyncio.get_running_loop()
            task = loop.create_task(_broadcast(announce))
            _pending.add(task)
            task.add_done_callback(_pending.discard)
    except Exception:  # never fails the committed request
        logger.debug("contract cache shadow invalidation failed", exc_info=True)


_pending: set[asyncio.Task[None]] = set()


def _cache_provider() -> Any:
    return _access.cache()


async def _broadcast(messages: list[dict[str, Any]]) -> None:
    try:
        provider = _cache_provider()
        raw_client = getattr(provider, "raw_client", None)
        if raw_client is None:
            return
        client = await raw_client()
        for message in messages:
            await client.publish(INVALIDATION_CHANNEL, json.dumps(message))
    except Exception:  # lost broadcast: TTL bounds it (measured)
        logger.debug("contract cache shadow broadcast failed", exc_info=True)


def _ensure_listener(observer: ShadowContractCache) -> None:
    global _listener
    if not observer.invalidation_channels():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - sync caller
        return
    if _listener is not None and _listener[1] is loop and not _listener[0].done():
        return
    try:
        provider = _cache_provider()
    except Exception:  # noqa: BLE001 - no cache: in-process invalidation only
        return
    if getattr(provider, "raw_client", None) is None:
        return
    stop = asyncio.Event()
    task = loop.create_task(
        _listen(observer, provider, stop), name="sandloom-contract-cache-shadow"
    )
    _listener = (task, loop, stop)


async def _listen(
    observer: ShadowContractCache, provider: Any, stop: asyncio.Event
) -> None:
    backoff = 0.5
    while not stop.is_set():
        pubsub: Any | None = None
        try:
            client = await provider.raw_client()
            pubsub = client.pubsub()
            await pubsub.subscribe(INVALIDATION_CHANNEL)
            observer.flush("subscribed")
            backoff = 0.5
            while not stop.is_set():
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message and message.get("type") == "message":
                    try:
                        data = json.loads(message.get("data"))
                        observer.invalidate(str(data["c"]), dict(data["p"]))
                    except Exception:  # noqa: BLE001 - malformed: flush
                        observer.flush("malformed")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - reconnect; flush what we hold
            observer.flush("subscription_lost")
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except TimeoutError:
                pass
            backoff = min(backoff * 2, 10.0)
        finally:
            if pubsub is not None:
                try:
                    await pubsub.aclose()
                except Exception:  # noqa: BLE001, S110 - best-effort pubsub teardown
                    pass


async def stop_listener() -> None:
    """Stop this process's invalidation listener (tests / shutdown)."""
    global _listener
    current_listener, _listener = _listener, None
    if current_listener is None:
        return
    task, loop, stop = current_listener
    stop.set()
    if loop is not asyncio.get_running_loop() or task.done():
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
    except TimeoutError:
        task.cancel()
    except Exception:  # noqa: BLE001, S110 - best-effort listener shutdown
        pass


__all__ = [
    "INVALIDATION_CHANNEL",
    "OUTCOMES",
    "OVER_GRANT_KINDS",
    "ShadowRequestMiddleware",
    "ShadowContractCache",
    "ShadowContractConfig",
    "build_from_settings",
    "configure",
    "current",
    "for_contract",
    "note_committed_events",
    "observe_read",
    "reset",
    "stop_listener",
]


_arm_commit_hook()
