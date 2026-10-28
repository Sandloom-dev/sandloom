"""Billing module events: typed producers of the ``billing.*.v1`` channels.

Every channel carries the common payload of BILLING_SPEC §7 (``event_id``,
``occurred_at``, ``customer_id``, the billing subject ``tenant_type`` /
``tenant_id``, and the channel's ``data``). Additive-only within v1. Publish
with ``await events.billing_event(channel, ...).publish()`` (or the class)
inside the change's unit (transactional outbox).
"""

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import Field

from sandloom.sdk import Event

SUBSCRIPTION_CREATED_V1 = "billing.subscription.created.v1"
SUBSCRIPTION_UPDATED_V1 = "billing.subscription.updated.v1"
SUBSCRIPTION_CANCELED_V1 = "billing.subscription.canceled.v1"
CREDITS_ADDED_V1 = "billing.credits.added.v1"
CREDITS_SPENT_V1 = "billing.credits.spent.v1"
#: Credits taken back because their payment was refunded / disputed
#: (``data``: amount, balance — may be negative —, reason).
CREDITS_REVERSED_V1 = "billing.credits.reversed.v1"
#: Reversed credits given back (a won dispute); ``data`` as credits.added.
CREDITS_RESTORED_V1 = "billing.credits.restored.v1"


class BillingEvent(Event):
    """The common payload of every billing channel (BILLING_SPEC §7)."""

    event_id: UUID = Field(default_factory=uuid4)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    customer_id: UUID
    tenant_type: Literal["user", "organization"]
    tenant_id: UUID
    data: dict[str, Any] = Field(default_factory=dict)


class SubscriptionCreated(BillingEvent, channel=SUBSCRIPTION_CREATED_V1):
    """A subscription started (``data``: status, PSP id, grants_access...)."""


class SubscriptionUpdated(BillingEvent, channel=SUBSCRIPTION_UPDATED_V1):
    """A subscription changed status or terms."""


class SubscriptionCanceled(BillingEvent, channel=SUBSCRIPTION_CANCELED_V1):
    """A subscription ended."""


class CreditsAdded(BillingEvent, channel=CREDITS_ADDED_V1):
    """Credits were granted (``data``: amount, balance, reason)."""


class CreditsSpent(BillingEvent, channel=CREDITS_SPENT_V1):
    """Credits were spent (``data``: amount, balance, reason)."""


class CreditsReversed(BillingEvent, channel=CREDITS_REVERSED_V1):
    """Credits were taken back after a refund or dispute."""


class CreditsRestored(BillingEvent, channel=CREDITS_RESTORED_V1):
    """Reversed credits were given back (a won dispute)."""


_BY_CHANNEL: dict[str, type[BillingEvent]] = {
    cls.channel: cls
    for cls in (
        SubscriptionCreated,
        SubscriptionUpdated,
        SubscriptionCanceled,
        CreditsAdded,
        CreditsSpent,
        CreditsReversed,
        CreditsRestored,
    )
}


def billing_event(
    channel: str,
    customer_id: UUID,
    tenant_type: str,
    tenant_id: UUID,
    data: dict[str, Any] | None = None,
) -> BillingEvent:
    """The typed event of ``channel`` (one of the billing channels above)."""
    return _BY_CHANNEL[channel].model_validate(
        {
            "customer_id": customer_id,
            "tenant_type": tenant_type,
            "tenant_id": tenant_id,
            "data": data or {},
        }
    )
