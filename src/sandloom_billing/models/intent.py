"""Operation intents: the authorized record of a class-F PSP operation.

ADR-0003 G1. A checkout / customer-portal request is authorized, its intent is
COMMITTED (``pending``), and only then is the PSP called — with
``psp_idempotency_key`` (derived from the intent id), so a retry of the same
request reconciles with the same PSP session instead of creating another. After
the PSP call the authority is re-verified and the intent becomes ``issued``
(the URL is returned) or ``revoked`` (withheld; a checkout session is expired
at the PSP). A PSP definitive rejection is ``failed``. The webhook honours a
completed checkout only for an ``issued`` intent.

Isolation: like every billing table, NOT ``TenantScoped`` — billing's tenant
is ``user`` XOR ``organization`` (two kinds), and every read starts from a
customer / intent resolved by billing's single tenant gate
(``services.tenant_resolver``) or by the PSP's ids in system webhook code.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, CreatedAt, new_id
from sandloom.sdk.data import UpdatedAt

INTENT_PENDING = "pending"
INTENT_ISSUED = "issued"
INTENT_REVOKED = "revoked"
INTENT_FAILED = "failed"

#: Statuses that still own their request key (a retry reuses the intent).
LIVE_INTENT_STATUSES = (INTENT_PENDING, INTENT_ISSUED)

OPERATION_CHECKOUT = "checkout"
OPERATION_CUSTOMER_PORTAL = "customer_portal"

#: Metadata key carrying the intent id on PSP sessions / subscriptions.
INTENT_METADATA_KEY = "sandloom_intent_id"


class OperationIntent(Base):
    """One authorized PSP operation (checkout / customer portal)."""

    __tablename__ = "billing_operation_intents"
    __table_args__ = (
        # One LIVE intent per (actor, operation, request Idempotency-Key): a
        # retry of the same request finds it; a revoked / failed one frees the
        # key (its PSP session is dead), so a later retry starts afresh.
        Index(
            "uq_billing_intent_live_request",
            "actor_id",
            "operation",
            "request_key",
            unique=True,
            postgresql_where=text("status IN ('pending', 'issued')"),
        ),
        # The periodic reconciler's two selections (IntentReconciler): stale
        # pending intents, and revoked checkouts whose session expire failed.
        # Both rows are rare and transient, so the partial indexes stay tiny
        # while the table grows with every checkout / portal session.
        Index(
            "ix_billing_intents_pending_created",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_billing_intents_owed_expiry",
            "created_at",
            postgresql_where=text(
                "status = 'revoked' AND operation = 'checkout' "
                "AND psp_expired_at IS NULL AND reconcile_attempts > 0"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=new_id)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID,
        ForeignKey("billing_customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: ``user`` | ``organization`` and its id (the billing tenant).
    tenant_type: Mapped[str] = mapped_column(String(16), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID, nullable=False)
    #: The user who was authorized (references identity by plain UUID).
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID, nullable=False)
    #: sha256 of the request's Idempotency-Key (NULL: no key, never reused).
    request_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: sha256 of the operation's parameters (tenant, price).
    params_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    price_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: ``tenancy.membership.v1`` membership ``version`` the action was
    #: authorized at (organization context; NULL for the personal tenant).
    authorized_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    psp_idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    psp_session_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )
    #: Checkout only (a portal URL is a bearer credential: never stored).
    psp_session_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: When the intent became ``issued`` (its URL was handed out). A portal
    #: intent's ``issued_at`` attributes later subscription changes of its
    #: customer (ADR-0003 open item 10).
    issued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: When the PSP will expire the checkout session (ADR-0003 open item 13):
    #: a retry of an issued checkout past it answers 410, never a dead URL.
    #: NULL on intents issued before it was stored (the PSP default applies).
    psp_session_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: When billing confirmed the checkout session can no longer be paid (our
    #: expire succeeded, or the PSP reported it expired). NULL on a revoked
    #: intent with a session = its compensation is still owed (#72).
    psp_expired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Failed reconciliation steps (PSP lookup of a stale pending intent, or
    #: the expire compensation of a revoked one) and the last error; bounded
    #: by BILLING_INTENT_RECONCILE_MAX_ATTEMPTS, then ``followup_at``.
    reconcile_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    reconcile_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Why the intent was revoked / failed (operator-facing, no PII).
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Set when the PSP reported a completion this intent did not authorize
    #: (revoked / pending): operator follow-up (refund is manual).
    followup_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]
