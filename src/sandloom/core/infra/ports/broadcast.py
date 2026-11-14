"""The ``broadcast`` capability (kernel-facing; spec section 6).

Online-only, at-most-once fan-out of byte payloads on named channels.

- A message reaches the subscriptions live when it is published: no
  persistence, no replay, no acknowledgement. A successful ``publish`` means the
  backend accepted it, not that anyone received it.
- **No silent loss on a live subscription.** A subscription delivers every
  message published to its channels while it is live, or its iterator raises
  ``Unavailable`` (a buffer overflow, a lost connection); a driver never skips a
  message and carries on, and never reconnects silently behind an open
  iterator. The subscriber resubscribes and treats the interval as a gap.
- Messages of one publisher on one channel arrive in publish order.
- A channel ending in ``.*`` matches every channel with that prefix, only with
  ``pattern_subscribe``; without it ``subscribe`` raises
  ``Unsupported("pattern_subscribe")``.
- Every driver accepts payloads of at least :data:`MIN_PAYLOAD_LIMIT_BYTES`;
  above its limit ``publish`` raises ``TooLarge``.

Errors: ``publish`` raises ``InvalidArgument`` (empty channel), ``TooLarge``,
``Unavailable``, ``Timeout``; ``subscribe`` raises ``InvalidArgument``,
``Unsupported("pattern_subscribe")``, ``Unavailable``; iteration raises
``Unavailable`` on a lost connection.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import ClassVar, Protocol

from sandloom.core.infra.ports._base import CapabilitySpec, InfraProvider

#: The payload size every driver accepts (1 MiB).
MIN_PAYLOAD_LIMIT_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class BroadcastMessage:
    """One message received on a subscription."""

    channel: str
    payload: bytes


class Subscription(Protocol):
    """A live subscription: iterate it for messages, ``aclose`` to end it."""

    def __aiter__(self) -> AsyncIterator[BroadcastMessage]: ...
    async def aclose(self) -> None: ...


class Broadcast(Protocol):
    """The operations the kernel calls on a ``broadcast`` instance."""

    async def publish(self, channel: str, payload: bytes) -> None: ...
    async def subscribe(self, channels: Sequence[str]) -> Subscription: ...


class BroadcastProvider(InfraProvider, Broadcast):
    """Base class of a ``broadcast`` driver's provider."""

    capability: ClassVar[str] = "broadcast"

    @abc.abstractmethod
    async def publish(self, channel: str, payload: bytes) -> None:
        """Hand ``payload`` to the subscriptions of ``channel`` live now."""

    @abc.abstractmethod
    async def subscribe(self, channels: Sequence[str]) -> Subscription:
        """Start a subscription to ``channels``."""


BROADCAST_CAPABILITY = CapabilitySpec(
    name="broadcast",
    version="v1",
    provider_contract=BroadcastProvider,
    feature_kinds={
        # ``subscribe`` accepts ``<prefix>.*`` channels.
        "pattern_subscribe": "behaviour",
        # Subscribers in every process of the deployment receive a message
        # (memory reaches only its own process).
        "shared": "guarantee",
    },
    description="Online at-most-once fan-out (contract-cache shadow, volatile bus).",
    # Kernel-facing: a module resolve or requirement is refused (B041).
    kernel_only=True,
)


__all__ = [
    "BROADCAST_CAPABILITY",
    "MIN_PAYLOAD_LIMIT_BYTES",
    "Broadcast",
    "BroadcastMessage",
    "BroadcastProvider",
    "Subscription",
]
