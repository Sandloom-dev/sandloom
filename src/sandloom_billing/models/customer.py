"""Billing customer: tenant (user XOR organization) ↔ PSP link."""

import uuid

from sqlalchemy import CheckConstraint, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, new_id


class BillingCustomer(Base):
    """Payment subject. Exactly one of user_id / organization_id is set.

    Ids reference identity/tenancy entities by UUID without FK constraints,
    keeping the module schema-independent (microservice-ready).
    """

    __tablename__ = "billing_customers"
    __table_args__ = (
        CheckConstraint(
            "(user_id IS NOT NULL) != (organization_id IS NOT NULL)",
            name="ck_billing_customer_one_of_tenant",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=new_id)
    stripe_customer_id: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID, unique=True, nullable=True)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, unique=True, nullable=True
    )

    @property
    def tenant_type(self) -> str:
        return "user" if self.user_id is not None else "organization"

    @property
    def tenant_id(self) -> uuid.UUID:
        tid = self.user_id if self.user_id is not None else self.organization_id
        assert tid is not None  # noqa: S101 - type narrowing: guaranteed by CHECK constraint
        return tid
