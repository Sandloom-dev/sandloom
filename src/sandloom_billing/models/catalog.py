"""Billing catalog: products and prices."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sandloom.sdk import Base

PRODUCT_TYPE_SUBSCRIPTION = "subscription"
PRODUCT_TYPE_ONE_TIME = "one_time"

INTERVAL_MONTH = "month"
INTERVAL_YEAR = "year"


class BillingProduct(Base):
    """Sellable product (subscription plan or one-time purchase)."""

    __tablename__ = "billing_products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stripe_product_id: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PRODUCT_TYPE_SUBSCRIPTION
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Loaded only where it is read (``list_plans`` uses selectinload): an
    # eager default cost every product read — the webhook's price lookup,
    # plan info — an extra SELECT. Accessing it unloaded raises instead of
    # emitting hidden SQL.
    prices: Mapped[list["BillingPrice"]] = relationship(
        back_populates="product", lazy="raise_on_sql"
    )


class BillingPrice(Base):
    """Concrete price point for a product (amount in minor units)."""

    __tablename__ = "billing_prices"
    __table_args__ = (
        UniqueConstraint(
            "product_id",
            "currency",
            "recurring_interval",
            name="uq_billing_price_product_currency_interval",
        ),
        CheckConstraint("amount >= 0", name="ck_billing_price_amount_non_negative"),
        CheckConstraint(
            "credits IS NULL OR credits > 0", name="ck_billing_price_credits_positive"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stripe_price_id: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True, index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("billing_products.id", ondelete="CASCADE"), nullable=False
    )
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    recurring_interval: Mapped[str | None] = mapped_column(
        String(10), nullable=True
    )  # month | year | NULL (one-time)
    # Credits granted by ONE purchase of this (one-time) price. The webhook
    # resolves the grant from here by price id — never from checkout metadata,
    # which would let whoever controls the session decide the amount.
    credits: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    product: Mapped[BillingProduct] = relationship(back_populates="prices")
