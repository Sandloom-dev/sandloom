"""A customer's prepaid balance and the charges made against it."""

import uuid

from sqlalchemy import Boolean, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, CreatedAt, TenantScoped, new_id


class PaymentsAccount(TenantScoped, Base):
    __tablename__ = "payments_accounts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "customer_id", name="uq_payments_accounts_customer"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=new_id)
    # The customer is another module's entity: a plain UUID, never a FK.
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID, nullable=False)
    balance: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class PaymentsCharge(TenantScoped, Base):
    __tablename__ = "payments_charges"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=new_id)
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID, nullable=False, index=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    # The caller's operation key: one charge per key (the command log already
    # dedupes; the unique column makes the row itself say so).
    operation_key: Mapped[str] = mapped_column(String(400), nullable=False, unique=True)
    refunded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[CreatedAt]
