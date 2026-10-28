"""What a durable message transport can do, declared instead of discovered.

The Streams bus used to feature-detect its transport at every use with
``getattr`` / ``inspect.signature`` (does it renew leases? batch acks? take
``block_ms``?), and each feature added another probe. A transport now
DECLARES its features as a :data:`TransportCapabilities` frozenset on its own
class::

    class MyTransport(MessageTransport):
        capabilities = frozenset({TransportCapability.BLOCKING_CONSUME, ...})

and the bus resolves them ONCE, at construction, with
:func:`transport_capabilities`.

A transport class that declares nothing — a third-party transport, a
duck-typed one, or a subclass of a built-in transport that adds or changes
methods without redeclaring — keeps working: its capabilities are probed
from its methods exactly as the bus used to (:func:`probe_transport_capabilities`).
A declaration is only trusted on the class that makes it, never inherited,
because a subclass may add (``renew_pending``) or narrow (``consume`` without
``block_ms``) what its parent declared.
"""

from __future__ import annotations

import inspect
from enum import Enum
from typing import Any

from sandloom.core.eventing.transport_base import MessageTransport


class TransportCapability(str, Enum):
    """One optional feature of a :class:`MessageTransport`."""

    #: ``consume(..., block_ms=)`` waits server-side for new entries.
    BLOCKING_CONSUME = "blocking_consume"
    #: ``ensure_group(..., start_id=)`` picks where a NEW group starts.
    GROUP_START_ID = "group_start_id"
    #: ``publish_many`` exists (otherwise the bus publishes one at a time).
    BATCH_PUBLISH = "batch_publish"
    #: ``publish_many`` is ONE round trip (overrides the looping default).
    NATIVE_BATCH_PUBLISH = "native_batch_publish"
    #: ``ack_many`` exists (otherwise the bus acks one delivery at a time).
    BATCH_ACK = "batch_ack"
    #: ``renew_pending`` + ``renew_interval_seconds``: in-flight batch leases.
    LEASE_RENEWAL = "lease_renewal"
    #: ``claim_group_owner`` records / verifies a group's owner.
    GROUP_OWNERSHIP = "group_ownership"
    #: ``never_dead_letter`` exempts a group from count-based dead-lettering.
    NEVER_DEAD_LETTER = "never_dead_letter"
    #: ``group_info`` reports a stream's groups (lag, pending, position).
    GROUP_INFO = "group_info"
    #: ``oldest_unacked_age_seconds`` measures a group's oldest unacked entry.
    OLDEST_UNACKED_AGE = "oldest_unacked_age"
    #: ``register_consumer`` heartbeats the shared subscription inventory.
    REGISTER_CONSUMER = "register_consumer"
    #: ``list_consumers`` reads the shared subscription inventory.
    LIST_CONSUMERS = "list_consumers"


#: The capability set a transport declares (``capabilities = frozenset({...})``).
TransportCapabilities = frozenset[TransportCapability]

#: Capabilities that are present iff the transport has the named method.
_METHOD_CAPABILITIES: tuple[tuple[TransportCapability, str], ...] = (
    (TransportCapability.BATCH_PUBLISH, "publish_many"),
    (TransportCapability.BATCH_ACK, "ack_many"),
    (TransportCapability.LEASE_RENEWAL, "renew_pending"),
    (TransportCapability.GROUP_OWNERSHIP, "claim_group_owner"),
    (TransportCapability.NEVER_DEAD_LETTER, "never_dead_letter"),
    (TransportCapability.GROUP_INFO, "group_info"),
    (TransportCapability.OLDEST_UNACKED_AGE, "oldest_unacked_age_seconds"),
    (TransportCapability.REGISTER_CONSUMER, "register_consumer"),
    (TransportCapability.LIST_CONSUMERS, "list_consumers"),
)


def _parameters(method: Any) -> set[str]:
    try:
        return set(inspect.signature(method).parameters)
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        return set()


def probe_transport_capabilities(transport: Any) -> TransportCapabilities:
    """Derive the capabilities of an undeclared transport from its methods.

    The conservative default for transports that declare nothing: exactly the
    feature detection the Streams bus always did (method presence, keyword
    parameters, an overridden ``publish_many``), run once.
    """
    caps = {
        cap
        for cap, name in _METHOD_CAPABILITIES
        if getattr(transport, name, None) is not None
    }
    if "block_ms" in _parameters(getattr(transport, "consume", None)):
        caps.add(TransportCapability.BLOCKING_CONSUME)
    group_params = _parameters(getattr(transport, "ensure_group", None))
    if "start_id" in group_params:
        caps.add(TransportCapability.GROUP_START_ID)
    method = getattr(type(transport), "publish_many", None)
    if callable(method) and method is not MessageTransport.publish_many:
        caps.add(TransportCapability.NATIVE_BATCH_PUBLISH)
    return frozenset(caps)


def transport_capabilities(transport: Any) -> TransportCapabilities:
    """The capabilities ``transport``'s own class declares, else probed ones."""
    declared = vars(type(transport)).get("capabilities")
    if declared is not None:
        return frozenset(declared)
    return probe_transport_capabilities(transport)


__all__ = [
    "TransportCapabilities",
    "TransportCapability",
    "probe_transport_capabilities",
    "transport_capabilities",
]
