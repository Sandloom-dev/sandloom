"""The kernel's durable token-revocation applier and the issuer's obligation
(ADR-0012 decision 5).

* **Applier.** Every composition registers the durable consumer of
  ``core.auth.tokens_revoked.v1``, independent of any module: a revocation
  mark that could not be written after commit (cache outage) is written by
  this consumer, redelivered until it is. Its group is
  ``sandloom:auth@tokens_revoked@<SANDLOOM_SERVICE_NAME>``: every service
  applies every revocation into its own cache, so two independent receivers
  must never share one group, and the replicas of one service must.
* **Service name, required.** On a cross-process event bus the group needs
  ``SANDLOOM_SERVICE_NAME``; there is no fallback to the database identity
  (two services may share one database and one bus). A process without it
  refuses to start in every entry point (:func:`revocation_applier_config_problem`
  runs in ``configuration_problems``). The in-process memory bus has no other
  receiver, so the applier runs there under the ``local`` group without one.
* **Readiness** (:func:`revocation_readiness`): ``/ready`` fails when the
  applier is needed and missing, when the oldest unacknowledged revocation or
  block event is older than the access-token lifetime, when such an event is
  dead-lettered, or when the consumer is not running. A younger backlog is
  transient: ready, exported as ``sandloom_consumer_lag_seconds`` and logged
  at WARNING above half the bound.
* **Issuer obligation** (:func:`issuer_obligation_problems`, SANDLOOM-B035):
  an ``issues_tokens=True`` module registers a durable consumer of
  ``core.auth.user_blocked.v1`` (it ends its refresh sessions on a block) or
  declares ``refresh_sessions=False``, which is verified against its refresh
  routes, session tables and consumers.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from prometheus_client import Gauge

import sandloom.core.auth.revocation as _revocation
from sandloom.core.auth.blocks import USER_BLOCKED_CHANNEL
from sandloom.core.auth.marks import TOKENS_REVOKED_CHANNEL
from sandloom.core.config.common import INVALID_CONFIGURATION, ConfigurationError
from sandloom.core.consumers.callbacks import EventConsumerDescriptor, EventEnvelope
from sandloom.core.descriptors.module_names import module_name

logger = logging.getLogger("sandloom.core.auth.applier")

#: The setting that names this deployment's consumer group.
SERVICE_NAME_SETTING = "SANDLOOM_SERVICE_NAME"
#: The applier's consumer name (``/diagnostics/consumers``).
APPLIER_NAME = "auth@tokens_revoked"
#: Group prefix; the service name follows.
APPLIER_GROUP_PREFIX = "sandloom:auth@tokens_revoked@"
#: The group suffix on the in-process memory bus (no other receiver exists).
LOCAL_DEPLOYMENT = "local"
#: The channels whose backlog and dead letters ``/ready`` judges (A9).
REVOCATION_CHANNELS = (TOKENS_REVOKED_CHANNEL, USER_BLOCKED_CHANNEL)
#: The issuer obligation and its verification (ADR-0012 decision 5).
ISSUER_CODE = "SANDLOOM-B035"

_GROUP_FORBIDDEN = frozenset(" \t\r\n|")
_REFRESH_WORDS = ("refresh",)
_SESSION_TABLE_WORDS = ("refresh", "session")

_GAUGE_CONSUMER_LAG = Gauge(
    "sandloom_consumer_lag_seconds",
    "Age of the oldest unacknowledged token-revocation or user-block event of "
    "a local consumer (0 = none pending); /ready fails past the access-token "
    "lifetime",
    ["consumer", "channel"],
    multiprocess_mode="livemax",
)


def cross_process_bus(cfg: Any) -> bool:
    """Whether ``cfg`` selects an event bus other processes share.

    ``EVENT_BUS_BACKEND=redis|streams``, or ``auto`` in production (which
    selects Streams). ``memory`` (and ``auto`` outside production) is the
    in-process bus: nothing else receives its events.
    """
    backend = str(getattr(cfg, "event_bus_backend", "auto") or "auto")
    if backend == "auto":
        return bool(getattr(cfg, "is_prod", False))
    return backend != "memory"


def applier_service_name(cfg: Any) -> str | None:
    """The configured ``SANDLOOM_SERVICE_NAME``, stripped (``None`` when unset)."""
    raw = getattr(cfg, "sandloom_service_name", None)
    name = str(raw).strip() if raw is not None else ""
    return name or None


def applier_group(cfg: Any) -> str:
    """The applier's consumer group for ``cfg``.

    ``sandloom:auth@tokens_revoked@<SANDLOOM_SERVICE_NAME>``; on the in-process
    bus without a name, ``…@local``. Never derived from the database: two
    services sharing one database would merge into one group.
    """
    name = applier_service_name(cfg)
    if name is None:
        if cross_process_bus(cfg):
            raise ConfigurationError(_missing_name_problem())
        name = LOCAL_DEPLOYMENT
    return APPLIER_GROUP_PREFIX + name


def _missing_name_problem() -> str:
    return (
        f"{SERVICE_NAME_SETTING} is not set: the kernel's durable revocation "
        f"applier ({APPLIER_NAME} on {TOKENS_REVOKED_CHANNEL}) runs in its own "
        f"consumer group per deployment ({APPLIER_GROUP_PREFIX}<service>), and "
        "without a name two services on this event bus would share one group "
        "and each miss the other's revocations. Set "
        f"{SERVICE_NAME_SETTING} to this service's name, the same for all its "
        "replicas and distinct from every other service on the bus "
        "(ADR-0012 decision 5)"
    )


def revocation_applier_config_problem(cfg: Any) -> str | None:
    """Why ``cfg`` cannot run the revocation applier (``None``: it can).

    Run by ``configuration_problems``, so the app, the worker,
    ``sandloom migrate`` and the configured CLI commands all refuse the same
    way. In every environment: a silent group merge is the failure.
    """
    if not cross_process_bus(cfg):
        return None
    name = applier_service_name(cfg)
    if name is None:
        return _missing_name_problem()
    if _GROUP_FORBIDDEN & set(name):
        return (
            f"{SERVICE_NAME_SETTING}={name!r} names the revocation applier's "
            "consumer group and must not contain whitespace or '|'"
        )
    return None


async def apply_token_revocation(session: Any, envelope: EventEnvelope) -> None:
    """``core.auth.tokens_revoked.v1`` consumer: the kernel's durable applier.

    Writes the payload's mark when the post-commit write failed and raises
    while it still cannot, so the delivery is retried (``dead_letter=False``:
    forever, capped backoff). Marks are monotonic: re-applying is a no-op.
    ``session`` is the delivery's unit (the exactly-once claim lives there).
    """
    del session  # the mark is a cache write; the unit holds the event claim
    payload = envelope.get("payload")
    await _revocation.apply_pending_revocation(
        dict(payload) if isinstance(payload, Mapping) else {}
    )


def applier_descriptor(cfg: Any) -> EventConsumerDescriptor:
    """The applier's consumer descriptor for ``cfg`` (see :func:`applier_group`)."""
    return EventConsumerDescriptor(
        name=APPLIER_NAME,
        channel=TOKENS_REVOKED_CHANNEL,
        handler=apply_token_revocation,
        module="sandloom",
        group=applier_group(cfg),
        dead_letter=False,
    )


def register_kernel_revocation_applier(core: Any, cfg: Any) -> EventConsumerDescriptor:
    """Register the applier on ``core`` like any durable consumer; return it.

    Every composition registers it (one consumer per deployment), so no
    module has to be loaded for a deferred revocation to be applied.
    """
    descriptor = applier_descriptor(cfg)
    core.register_event_consumer(descriptor)
    return descriptor


# --------------------------------------------------------------------------- #
# the issuer's obligation (SANDLOOM-B035)
# --------------------------------------------------------------------------- #


def _owned_by(consumer_module: Any, path: str) -> bool:
    return consumer_module in {path, module_name(path)}


def _refresh_route(path: str) -> bool:
    segments = [s.lower() for s in path.split("/") if s]
    return any(word in s for s in segments for word in _REFRESH_WORDS)


def _session_table(table: str) -> bool:
    lowered = table.lower()
    return any(word in lowered for word in _SESSION_TABLE_WORDS)


def issuer_obligation_problems(
    descriptors: Iterable[Any],
    consumers: Iterable[Any],
    manifest: Any = None,
) -> list[str]:
    """Every issuer that cannot meet its obligation (empty: all can).

    ``descriptors`` are the loaded modules' descriptors, ``consumers`` the
    registered event-consumer descriptors and ``manifest`` the application
    manifest (routes and tables per module). An ``issues_tokens=True`` module
    must register a durable consumer of ``core.auth.user_blocked.v1`` unless
    it declares ``refresh_sessions=False``; that declaration is refused when
    the module still registers a refresh route, a refresh-session table or a
    ``user_blocked`` consumer.
    """
    consumer_list = list(consumers)
    entries = {
        str(getattr(m, "name", "")): m for m in getattr(manifest, "modules", ()) or ()
    }
    problems: list[str] = []
    for descriptor in descriptors:
        if not getattr(descriptor, "issues_tokens", False):
            continue
        path = str(descriptor.name)
        blocked = [
            c
            for c in consumer_list
            if getattr(c, "channel", None) == USER_BLOCKED_CHANNEL
            and _owned_by(getattr(c, "module", None), path)
        ]
        if getattr(descriptor, "refresh_sessions", True):
            if not any(getattr(c, "durable", True) for c in blocked):
                problems.append(
                    f"module {path!r} issues tokens but registers no durable "
                    f"consumer of {USER_BLOCKED_CHANNEL}: an emergency block must "
                    "end its refresh sessions. Register one, or declare "
                    "refresh_sessions=False if it holds none"
                )
            continue
        problems.extend(_stateless_contradictions(path, blocked, entries.get(path)))
    return problems


def _stateless_contradictions(
    path: str, blocked: Sequence[Any], entry: Any
) -> list[str]:
    """What contradicts ``path``'s ``refresh_sessions=False`` declaration."""
    found: list[str] = []
    if blocked:
        names = sorted(str(getattr(c, "name", "")) for c in blocked)
        found.append(f"a consumer of {USER_BLOCKED_CHANNEL} ({', '.join(names)})")
    routes = sorted(
        {
            str(r.path)
            for r in getattr(entry, "routes", ()) or ()
            if _refresh_route(str(r.path))
        }
    )
    if routes:
        found.append(f"refresh route(s) {routes}")
    tables = sorted(
        {
            str(m.table)
            for m in getattr(entry, "models", ()) or ()
            if _session_table(str(m.table))
        }
    )
    if tables:
        found.append(f"session table(s) {tables}")
    return [
        f"module {path!r} declares refresh_sessions=False (a stateless issuer) "
        f"but registers {what}: drop the declaration and end its sessions on "
        f"{USER_BLOCKED_CHANNEL}, or remove what holds sessions"
        for what in found
    ]


def enforce_issuer_obligation(
    descriptors: Iterable[Any],
    consumers: Iterable[Any],
    manifest: Any,
    *,
    log: logging.Logger,
) -> None:
    """Refuse startup on any :func:`issuer_obligation_problems` finding."""
    problems = issuer_obligation_problems(descriptors, consumers, manifest)
    if not problems:
        return
    for problem in problems:
        log.error("token issuer: %s", problem)
    raise ConfigurationError(
        INVALID_CONFIGURATION
        + f"{ISSUER_CODE}: "
        + "; ".join(problems)
        + f" (`sandloom explain {ISSUER_CODE}`)"
    )


# --------------------------------------------------------------------------- #
# readiness (owner's confirmation A9)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RevocationReadiness:
    """What ``/ready`` concludes about the revocation and block consumers."""

    ready: bool
    reasons: tuple[str, ...] = ()
    #: consumer name -> age of its oldest unacknowledged event (seconds).
    lag_seconds: Mapping[str, float] = field(default_factory=dict)
    #: consumer name -> dead-lettered events of its channel and group.
    dead_letters: Mapping[str, int] = field(default_factory=dict)


def evaluate_revocation_readiness(
    statuses: Iterable[Any],
    dead_letters: Mapping[tuple[str, str], int],
    *,
    bound_seconds: float,
    needed: bool,
    expected_group: str | None,
) -> RevocationReadiness:
    """Judge the revocation / block consumers (pure; see the module doc).

    ``statuses`` are the consumer runtime's ``ConsumerStatus`` items;
    ``dead_letters`` counts dead-lettered events per ``(channel, group)``;
    ``bound_seconds`` is the access-token lifetime. Not ready when the
    applier is ``needed`` and absent, when one of these consumers is not
    running, when its oldest unacknowledged event is older than the bound,
    or when one of its events is dead-lettered.
    """
    relevant = [
        s for s in statuses if getattr(s, "channel", None) in REVOCATION_CHANNELS
    ]
    reasons: list[str] = []
    lags: dict[str, float] = {}
    letters: dict[str, int] = {}
    if needed and not any(
        s.channel == TOKENS_REVOKED_CHANNEL
        and (expected_group is None or s.group == expected_group)
        for s in relevant
    ):
        reasons.append(
            f"the revocation applier ({expected_group or APPLIER_NAME}) is not "
            "registered"
        )
    for status in relevant:
        name = str(status.group or status.name)
        if status.state != "running":
            reasons.append(f"{name} on {status.channel} is {status.state}")
        age = getattr(status, "oldest_unacked_age_seconds", None)
        if age is not None:
            lags[name] = float(age)
            if age > bound_seconds:
                reasons.append(
                    f"{name}: the oldest pending {status.channel} event is "
                    f"{age:.0f}s old, past the access-token lifetime "
                    f"({bound_seconds:.0f}s)"
                )
        count = int(dead_letters.get((status.channel, str(status.group)), 0))
        if count:
            letters[name] = count
            reasons.append(
                f"{name}: {count} {status.channel} event(s) dead-lettered "
                f"(`sandloom streams dlq-list {status.channel}`)"
            )
    return RevocationReadiness(
        ready=not reasons,
        reasons=tuple(reasons),
        lag_seconds=lags,
        dead_letters=letters,
    )


async def _dead_letters(
    bus: Any, groups: Iterable[tuple[str, str]]
) -> dict[tuple[str, str], int]:
    """Dead-lettered events per ``(channel, group)`` the bus can report.

    One ``list_dlq`` read per channel (Streams: an ``XRANGE`` of its DLQ);
    the in-process memory bus keeps its own list. Best-effort: a read that
    fails reports nothing (the consumer's state and lag still judge it).
    """
    wanted = set(groups)
    counts: dict[tuple[str, str], int] = {}
    transport = getattr(bus, "transport", None)
    list_dlq = getattr(transport, "list_dlq", None)
    if callable(list_dlq):
        for channel in sorted({channel for channel, _ in wanted}):
            try:
                rows = await list_dlq(channel, count=100)
            except Exception:
                logger.debug("dead-letter read failed for %s", channel, exc_info=True)
                continue
            for row in rows or ():
                key = (channel, str(row.get("group")))
                if key in wanted:
                    counts[key] = counts.get(key, 0) + 1
        return counts
    for channel, _message, _attempt in getattr(bus, "dead_letters", ()) or ():
        for key in wanted:
            if key[0] == channel:
                counts[key] = counts.get(key, 0) + 1
    return counts


class RevocationReadinessProbe:
    """``/ready``'s revocation check, its dead-letter read cached for ``ttl``."""

    def __init__(self, core: Any, cfg: Any, *, ttl: float) -> None:
        self._core = core
        self._cfg = cfg
        self._ttl = max(0.0, ttl)
        self._cached_at = 0.0
        self._cached: dict[tuple[str, str], int] = {}
        self._needed = cross_process_bus(cfg)
        name = applier_service_name(cfg)
        # Unnamed on a shared bus never boots (configuration_problems); judge
        # "missing" by channel alone then rather than refuse here.
        self._group = APPLIER_GROUP_PREFIX + name if self._needed and name else None

    async def __call__(self, consumer_runtime: Any) -> RevocationReadiness:
        """Judge the runtime's revocation and block consumers now."""
        snapshot = consumer_runtime.snapshot() if consumer_runtime is not None else None
        statuses = list(getattr(snapshot, "consumers", ()) or ())
        groups = [
            (s.channel, str(s.group))
            for s in statuses
            if s.channel in REVOCATION_CHANNELS
        ]
        now = time.monotonic()
        if groups and now - self._cached_at >= self._ttl:
            self._cached = await _dead_letters(self._core.event_bus, groups)
            self._cached_at = now
        bound = float(self._cfg.access_token_expire_minutes) * 60.0
        result = evaluate_revocation_readiness(
            statuses,
            self._cached,
            bound_seconds=bound,
            needed=self._needed,
            expected_group=self._group,
        )
        for name, age in result.lag_seconds.items():
            channel = next(
                (s.channel for s in statuses if str(s.group or s.name) == name), ""
            )
            _GAUGE_CONSUMER_LAG.labels(name, channel).set(age)
            if bound / 2 < age <= bound:
                logger.warning(
                    "%s: the oldest pending %s event is %.0fs old, over half "
                    "the access-token lifetime (%.0fs); /ready fails past it",
                    name,
                    channel,
                    age,
                    bound,
                )
        return result


__all__ = [
    "APPLIER_GROUP_PREFIX",
    "APPLIER_NAME",
    "ISSUER_CODE",
    "LOCAL_DEPLOYMENT",
    "REVOCATION_CHANNELS",
    "SERVICE_NAME_SETTING",
    "RevocationReadiness",
    "RevocationReadinessProbe",
    "applier_descriptor",
    "applier_group",
    "applier_service_name",
    "apply_token_revocation",
    "cross_process_bus",
    "enforce_issuer_obligation",
    "evaluate_revocation_readiness",
    "issuer_obligation_problems",
    "register_kernel_revocation_applier",
    "revocation_applier_config_problem",
]
