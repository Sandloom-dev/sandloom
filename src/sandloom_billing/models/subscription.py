"""Billing subscription model."""

import logging
import uuid
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base

logger = logging.getLogger("sandloom_billing.subscription")

STATUS_ACTIVE = "active"
STATUS_TRIALING = "trialing"
STATUS_PAST_DUE = "past_due"
STATUS_CANCELED = "canceled"
STATUS_UNPAID = "unpaid"

SUBSCRIPTION_STATUSES = (
    STATUS_ACTIVE,
    STATUS_TRIALING,
    STATUS_PAST_DUE,
    STATUS_CANCELED,
)

STATUS_INCOMPLETE = "incomplete"
STATUS_INCOMPLETE_EXPIRED = "incomplete_expired"
STATUS_PAUSED = "paused"

#: Every status Stripe can report for a subscription; anything else is
#: refused by the webhook (journaled ``failed``), never stored.
PSP_SUBSCRIPTION_STATUSES = frozenset(
    {
        STATUS_INCOMPLETE,
        STATUS_INCOMPLETE_EXPIRED,
        STATUS_TRIALING,
        STATUS_ACTIVE,
        STATUS_PAST_DUE,
        STATUS_CANCELED,
        STATUS_UNPAID,
        STATUS_PAUSED,
    }
)

#: Final states: Stripe never revives them (a resubscription gets a new id).
TERMINAL_STATUSES = frozenset({STATUS_CANCELED, STATUS_INCOMPLETE_EXPIRED})

#: Statuses a successful invoice payment may move back to ``active``.
RECOVERABLE_STATUSES = frozenset({STATUS_PAST_DUE, STATUS_UNPAID})

#: How a subscription row came to exist (ADR-0003 open item 9).
ORIGIN_INTENT = "intent"  # created by a webhook for an ISSUED operation intent
ORIGIN_PSP = "psp"  # created by a webhook WITHOUT an intent marker
ORIGIN_LEGACY = "legacy"  # existed before the policy (migration backfill)

#: Statuses that may grant access (subject to the period / past_due policy).
_ACCESS_STATUSES = frozenset({STATUS_ACTIVE, STATUS_TRIALING, STATUS_PAST_DUE})


class BillingSubscription(Base):
    """Subscription of a customer to a recurring price."""

    __tablename__ = "billing_subscriptions"
    __table_args__ = (
        Index(
            "uq_billing_subscriptions_intent_id",
            "intent_id",
            unique=True,
            postgresql_where=text("intent_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID,
        ForeignKey("billing_customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    price_id: Mapped[int | None] = mapped_column(
        ForeignKey("billing_prices.id", ondelete="SET NULL"), nullable=True
    )
    stripe_subscription_id: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    trial_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    canceled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    # Ordering guard for PSP webhooks: the ``created`` unix timestamp and id of
    # the last event applied to this row. Older events are ignored so an
    # out-of-order / replayed delivery can never roll the state back.
    last_event_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: ``intent`` | ``psp`` | ``legacy`` — see :data:`ORIGIN_PSP`. Syncing the
    #: PSP's state never depends on it; GRANTING does. Defaults are ``psp``
    #: (fail closed): a row inserted without an origin (old code during a
    #: rolling deploy, a manual INSERT) is intent-less and grants only under
    #: the explicit policy; ``legacy`` is set ONLY by migration 0005's backfill.
    origin: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ORIGIN_PSP,
        server_default=ORIGIN_PSP,
    )

    #: The checkout intent that authorized this subscription (``origin =
    #: 'intent'``); UNIQUE when set — one intent yields at most one
    #: subscription (ADR-0003 open item 16).
    intent_id: Mapped[uuid.UUID | None] = mapped_column(UUID, nullable=True)

    @property
    def is_terminal(self) -> bool:
        """A canceled / incomplete_expired PSP subscription never comes back
        (a new one gets a new id)."""
        return self.status in TERMINAL_STATUSES

    def grants_access(
        self,
        *,
        deny_on_past_due: bool,
        now: datetime | None = None,
        allow_intentless: bool = False,
    ) -> bool:
        """Spec §9: access computation (see :func:`subscription_grants_access`)."""
        return subscription_grants_access(
            self,
            deny_on_past_due=deny_on_past_due,
            now=now,
            allow_intentless=allow_intentless,
        )


class SubscriptionAccessView(Protocol):
    """What the access predicate reads of a subscription (the ORM row or the
    planner's ``SubscriptionRow`` DTO)."""

    @property
    def id(self) -> int: ...

    @property
    def status(self) -> str: ...

    @property
    def price_id(self) -> int | None: ...

    @property
    def origin(self) -> str: ...

    @property
    def current_period_end(self) -> datetime | None: ...

    @property
    def trial_end(self) -> datetime | None: ...

    @property
    def stripe_subscription_id(self) -> str | None: ...


def subscription_grants_access(
    sub: SubscriptionAccessView,
    *,
    deny_on_past_due: bool,
    now: datetime | None = None,
    allow_intentless: bool = False,
) -> bool:
    """Spec §9: access computation including grace-period policy.

    No known catalog price (``price_id`` NULL) → no access. An intent-less
    row (``origin='psp'``) → access only with ``allow_intentless``
    (``BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS``).

    A paying status WITHOUT a period end is a data error (e.g. a webhook
    parsed with the wrong API shape), never perpetual access: it denies and
    logs so the gap is visible instead of silently granting forever.
    """
    if sub.status not in _ACCESS_STATUSES:
        return False
    # Fail closed (ADR-0003 open item 11): a subscription to a price the
    # catalog does not know grants NOTHING — its state is synced, never an
    # entitlement nobody configured.
    if sub.price_id is None:
        return False
    # A subscription nobody authorized through Sandloom (no operation intent)
    # grants only under the explicit policy (ADR-0003 open item 9).
    if sub.origin == ORIGIN_PSP and not allow_intentless:
        return False
    if sub.status == STATUS_PAST_DUE and deny_on_past_due:
        return False

    now = now or datetime.now(UTC)
    period_end = sub.current_period_end
    if period_end is None and sub.status == STATUS_TRIALING:
        period_end = sub.trial_end
    if period_end is None:
        logger.error(
            "billing subscription %s (status=%s) has no current_period_end; "
            "denying access (data error)",
            sub.stripe_subscription_id or sub.id,
            sub.status,
        )
        return False
    if period_end.tzinfo is None:
        period_end = period_end.replace(tzinfo=UTC)
    return period_end > now
