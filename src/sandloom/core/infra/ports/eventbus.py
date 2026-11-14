"""The ``eventbus`` capability (kernel-facing; spec section 7).

Until ``idk-eventbus-contract`` replaces it here (``EventTransport``, the
feature vocabulary of spec 7), this is the ``EventBusProvider`` the kernel has
used so far, moved here unchanged from :mod:`sandloom.core.infra.contracts`
(which re-exports it), with the capability's spec.
"""

from __future__ import annotations

import abc
from typing import Any, ClassVar

from sandloom.core.infra.ports._base import CapabilitySpec, InfraProvider


class EventBusProvider(InfraProvider):
    """Pub/sub domain-event transport capability.

    Wraps an object implementing ``sandloom.core.eventing.event_bus.EventBusProtocol``
    so the
    bus participates in the same registry/lifecycle as other infrastructure.
    """

    capability: ClassVar[str] = "eventbus"

    @abc.abstractmethod
    def bus(self) -> Any:
        """Return the underlying event bus (EventBusProtocol)."""


EVENTBUS_CAPABILITY = CapabilitySpec(
    name="eventbus",
    version="v1",
    provider_contract=EventBusProvider,
    # Delivery semantics differ by driver: a module that needs durable /
    # at-least-once delivery (billing, orders, provisioning) requires it
    # explicitly, so it can never silently run on a volatile in-memory bus.
    # ``idk-eventbus-contract`` removes ``publish``, ``subscribe`` and
    # ``volatile`` and folds ``at_least_once`` and ``consumer_groups`` into
    # ``durable`` (spec 7).
    feature_kinds={
        "publish": "behaviour",
        "subscribe": "behaviour",
        "wildcard_subscribe": "behaviour",
        "volatile": "guarantee",
        "durable": "guarantee",
        "at_least_once": "guarantee",
        "consumer_groups": "guarantee",
    },
    description="Pub/sub domain-event transport.",
)


__all__ = ["EVENTBUS_CAPABILITY", "EventBusProvider"]
