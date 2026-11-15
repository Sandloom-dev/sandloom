"""The ``notification`` capability (module-facing, public; spec section 10).

Major 2: the shape of major 1 (``Notifier.send``), with the error list below in
place of "raises on a delivery failure", so a driver of major 1 is refused at
load (SANDLOOM-B022) instead of raising anything.
"""

from __future__ import annotations

import abc
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Protocol

from sandloom.core.infra.ports._base import CapabilitySpec, InfraProvider

#: Outbound delivery channels a notification can target.
NotificationChannel = Literal["email", "sms", "push", "inapp"]


@dataclass(frozen=True)
class Notification:
    """One outbound notification, independent of the delivery backend.

    A logical message: which ``channel`` to deliver on, the ``recipient``
    address for that channel (an email, phone, device token, or user id for
    ``inapp``), a short ``subject`` and ``body``, plus structured ``data`` and an
    optional ``dedupe_key`` a driver may use to suppress duplicate sends.

    ``body_fields`` are the values the ``body`` was built from, so a driver can
    render its own template. They are as sensitive as the body (a secret link, a
    name): a driver never logs them, and ``data`` stays safe to record.
    """

    channel: NotificationChannel
    recipient: str
    subject: str
    body: str
    data: Mapping[str, Any] = field(default_factory=dict)
    dedupe_key: str | None = None
    body_fields: Mapping[str, str] = field(default_factory=dict, repr=False)


class Notifier(Protocol):
    """What a module calls on ``notifier()``."""

    async def send(self, notification: Notification) -> None: ...


class NotifierProvider(InfraProvider, Notifier):
    """Outbound notification capability (email, SMS, push, in-app).

    Modules never import an email/SMS SDK: they build a :class:`Notification` and
    call :meth:`send`. A driver delivers it via its backend (SMTP, a provider
    API, a structured log in dev). The portable surface is a single ``send`` —
    drivers advertise which channels they actually deliver via :meth:`features`.
    """

    capability: ClassVar[str] = "notification"

    @abc.abstractmethod
    async def send(self, notification: Notification) -> None:
        """Hand ``notification`` to the backend for delivery.

        Returns once the backend accepted it (not a delivery receipt).
        ``dedupe_key`` is advisory. Raises (``sandloom.sdk.infra``)
        ``InvalidArgument`` (a recipient not valid for the channel),
        ``Unsupported(channel)`` (a channel the driver does not declare),
        ``TooLarge``, ``PermissionDenied``, ``Unavailable`` (the caller may
        retry) or ``Timeout``.
        """


NOTIFICATION_CAPABILITY = CapabilitySpec(
    name="notification",
    # Modules see the shape of v1; drivers owe the errors of major 2.
    version="v1",
    major=2,
    provider_contract=NotifierProvider,
    # Delivery channels a driver may support; a module that must send email
    # requires the ``email`` feature so it never silently runs on a driver that
    # cannot deliver it.
    feature_kinds={
        "email": "behaviour",
        "sms": "behaviour",
        "push": "behaviour",
        "inapp": "behaviour",
    },
    description="Outbound notifications (email / SMS / push / in-app).",
)


__all__ = [
    "NOTIFICATION_CAPABILITY",
    "Notification",
    "NotificationChannel",
    "Notifier",
    "NotifierProvider",
]
