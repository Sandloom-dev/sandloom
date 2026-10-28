"""Credits: materialized balance + append-only ledger."""

import uuid

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, CreatedAt
from sandloom.sdk.data import MoneyLedger, UpdatedAt


class CreditBalance(Base):
    """Materialized credit balance (derived from the ledger)."""

    __tablename__ = "billing_credit_balances"
    # No ``amount >= 0`` CHECK since 0008_billing (ADR-0003 open item 12): a
    # refund / dispute of a purchase whose credits were already spent reverses
    # them anyway and the balance goes NEGATIVE (owed). Spending stays guarded
    # by its own ``amount >= :n`` predicate, so only a reversal can produce a
    # negative balance, and balance == SUM(ledger.delta) always holds.

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID,
        ForeignKey("billing_customers.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    # BIGINT: a 32-bit balance overflowed into a 500 on large grants.
    amount: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class CreditLedgerEntry(MoneyLedger, Base):
    """Append-only journal of credit operations.

    Idempotent per customer: ``UNIQUE(customer_id, operation_key)``. Keys are
    scoped to the customer so one tenant's key can never collide with (and
    silently no-op) another tenant's operation. ``balance_after`` records the
    balance this operation produced, so an idempotent replay returns the
    ORIGINAL result rather than whatever the balance is now.
    """

    __tablename__ = "billing_credit_ledger"
    __operation_key__ = "operation_key"
    __table_args__ = (
        UniqueConstraint(
            "customer_id",
            "operation_key",
            name="uq_billing_credit_ledger_customer_operation",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # No standalone index: the idempotency UNIQUE (customer_id, operation_key)
    # leads with it and serves every per-customer lookup (dropped in 0003).
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID,
        ForeignKey("billing_customers.id", ondelete="CASCADE"),
        nullable=False,
    )
    delta: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    operation_key: Mapped[str] = mapped_column(String(255), nullable=False)
    balance_after: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[CreatedAt] = mapped_column(index=True)


#: Dispute states of a PSP payment (``None``: never disputed). An
#: ``inquiry`` (Stripe ``warning_*``) withdraws no funds and reverses nothing.
DISPUTE_INQUIRY = "inquiry"
DISPUTE_OPEN = "open"
DISPUTE_WON = "won"
DISPUTE_LOST = "lost"
#: Dispute states whose disputed amount is withdrawn from the merchant.
DISPUTE_WITHDRAWN = frozenset({DISPUTE_OPEN, DISPUTE_LOST})
#: A closed dispute never reopens.
DISPUTE_TERMINAL = frozenset({DISPUTE_WON, DISPUTE_LOST})


class CreditGrant(Base):
    """One credit-pack purchase: what was granted for which PSP payment.

    The link a refund / dispute (which names a charge's ``payment_intent``,
    never the checkout session) needs to find the credits to reverse
    (ADR-0003 open item 12). Written in the same transaction as the grant's
    ledger entry. ``reversed`` is the credits currently taken back by ledger
    entries of this grant; what SHOULD be reversed is derived from the PSP
    payment's state (:class:`PspPaymentState`, :func:`reversal_due`). Grants
    from before 0008_billing have no row: not reversed automatically.
    """

    __tablename__ = "billing_credit_grants"
    __table_args__ = (
        Index(
            "uq_billing_credit_grants_payment_intent",
            "psp_payment_intent_id",
            unique=True,
            postgresql_where=text("psp_payment_intent_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID,
        ForeignKey("billing_customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    psp_session_id: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True
    )
    psp_payment_intent_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    price_id: Mapped[int] = mapped_column(Integer, nullable=False)
    credits: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: What was paid (minor units; verified = the catalog price at grant).
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    #: Credits currently reversed by ledger entries of this grant.
    reversed: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class PspPaymentState(Base):
    """The latest known refund / dispute state of ONE PSP payment, keyed by
    its ``payment_intent`` — recorded whether or not a credit grant for it
    exists yet, so a refund / dispute delivered BEFORE the purchase's
    completion is applied when the grant is created (same transaction), not
    lost (ADR-0003 open item 12). Every field only moves forward: the refunded
    amount is cumulative (max), a closed dispute never reopens."""

    __tablename__ = "billing_psp_payment_states"

    psp_payment_intent_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    #: The PSP customer named by the refunded charge (a dispute names none);
    #: must be the grant's customer for anything to be reversed.
    psp_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    amount_refunded: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    dispute_status: Mapped[str | None] = mapped_column(String(8), nullable=True)
    #: The disputed amount (a dispute may cover part of the charge).
    dispute_amount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    updated_at: Mapped[UpdatedAt]


def reversal_due(grant: CreditGrant, state: PspPaymentState | None) -> int:
    """Credits of ``grant`` that should be reversed given its payment state.

    The money taken back is the cumulative refunded amount plus the disputed
    amount while the dispute is open or lost (an inquiry, a won dispute: 0),
    capped at what was paid; the credits follow proportionally, rounded UP,
    capped at what was granted — a refund plus a dispute never reverse more
    than was granted.
    """
    if state is None or int(grant.amount) <= 0:
        return 0
    money = int(state.amount_refunded)
    if state.dispute_status in DISPUTE_WITHDRAWN:
        money += int(state.dispute_amount)
    money = min(money, int(grant.amount))
    credits = int(grant.credits)
    return min(credits, -(-credits * money // int(grant.amount)))
