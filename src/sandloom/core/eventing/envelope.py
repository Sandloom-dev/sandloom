"""Event envelope contract and publish-side value types (no I/O)."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import (
    Any,
    Final,
    Literal,
    NamedTuple,
    TypedDict,
)

from sandloom.core.contracts.validators import ContractValidationError
from sandloom.core.observability.tracing import get_current_trace_id

#: ``prevalidated=PREFLIGHTED``: the payload passed THIS bus's ``preflight``
#: (channel, schema, size) and is unchanged since — no check is repeated.
PREFLIGHTED: Final = "preflighted"
#: ``prevalidated=`` of ``publish_event`` / ``publish_event_batch``.
Prevalidated = bool | Literal["preflighted"]

_VERSIONED_CHANNEL_RE = re.compile(r".*\.v[0-9]+(\.[0-9]+)?$")


def normalize_schema_version(channel: str, schema_version: str = "v1") -> str:
    """Derive the authoritative schema version for ``channel``.

    A channel ending in ``.vN`` is the source of truth for its version. The
    default ``"v1"`` is overridden to match the suffix; an explicit
    ``schema_version`` that CONTRADICTS a versioned suffix is rejected with
    ``ContractValidationError`` (e.g. ``"...v2"`` published with
    ``schema_version="v3"``) — that disagreement is a bug, not something to
    silently reconcile. This MUST be applied before an event is written to the
    transactional outbox (``di.publish_event``) so the stored row and the later
    dispatched envelope can never disagree on the version (diagnostics / replay /
    consumers would otherwise see two versions of one event).

    Note ``"v1"`` is the implicit default and cannot be told apart from an
    explicit ``"v1"``, so a ``.vN`` channel left at the default is normalized
    (not rejected); pass the version explicitly only when it matches the suffix.
    """
    suffix = channel.rsplit(".", 1)[-1]
    suffix_versioned = bool(re.fullmatch(r"v[0-9]+", suffix))
    if schema_version == "v1":
        return suffix if suffix_versioned else "v1"
    if suffix_versioned and schema_version != suffix:
        raise ContractValidationError(
            f"schema_version={schema_version!r} contradicts the versioned "
            f"channel suffix of {channel!r} (.{suffix}); pass a matching version "
            "or omit it to inherit the suffix"
        )
    return schema_version


class EventEnvelope(TypedDict):
    """Standardized event-bus message contract.

    All base fields are REQUIRED (no ``total=False``): every envelope carries a
    stable id (== outbox row id, for dedupe), channel, schema version, the
    originating trace id (nullable), an emission timestamp, and the payload.
    A producer cannot emit a half-formed envelope.
    """

    id: str
    channel: str
    schema_version: str
    trace_id: str | None
    emitted_at: str  # ISO 8601 UTC timestamp
    payload: dict[str, Any]


CallbackType = Callable[[EventEnvelope], Awaitable[None]]


class OutboundEvent(NamedTuple):
    """One event of a :meth:`BaseEventBus.publish_event_batch` call.

    The fields mirror :meth:`BaseEventBus.publish_event`'s arguments;
    ``event_id`` (the outbox row id) becomes the envelope id.
    """

    channel: str
    payload: dict[str, Any]
    schema_version: str = "v1"
    event_id: str | None = None


@dataclass(frozen=True, slots=True)
class BatchPublishResult:
    """Outcome of :meth:`BaseEventBus.publish_event_batch`.

    ``published`` LEADING events (in the given order) were handed to the
    transport; ``error`` is the failure that stopped the batch (``None`` when
    every event was published). Events past ``published`` are NOT delivered —
    some of them may have reached the broker anyway (an ambiguous transport
    error), so a retry may duplicate, never lose, an event.
    """

    published: int
    error: BaseException | None = None

    @property
    def complete(self) -> bool:
        return self.error is None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _safe_trace_id() -> str | None:
    # Safely extract trace_id; do not let it break publishing
    try:
        return get_current_trace_id()
    except Exception:  # pragma: no cover  # noqa: BLE001 - tracing must never break publishing
        return None


def _match_channel(sub: str, channel: str) -> bool:
    """Support exact match and the ``prefix.*`` pattern.

    Note: the pattern matching is simple — if more powerful matching is
    needed, extract it into a dedicated class/function.
    """
    if sub.endswith(".*"):
        prefix = sub[:-2]
        return channel.startswith(f"{prefix}.") or channel == prefix
    return sub == channel
