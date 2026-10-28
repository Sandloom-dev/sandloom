"""The ledger: balances, charges, refunds (all in the caller's UoW session)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import PaymentsAccount, PaymentsCharge


class InsufficientFunds(Exception):
    """A definitive business refusal (402): the saga compensates."""

    http_status = 402
    code = "insufficient_funds"


class Ledger:
    @classmethod
    async def _account(cls, db: AsyncSession, customer_id: UUID) -> PaymentsAccount:
        found = (
            await db.execute(
                select(PaymentsAccount)
                .where(PaymentsAccount.customer_id == customer_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if found is None:
            found = PaymentsAccount(customer_id=customer_id, balance=0)
            db.add(found)
            await db.flush()
        return found

    @classmethod
    async def balance(cls, db: AsyncSession, customer_id: UUID) -> int:
        found = (
            await db.execute(
                select(PaymentsAccount.balance).where(
                    PaymentsAccount.customer_id == customer_id
                )
            )
        ).scalar_one_or_none()
        return int(found or 0)

    @classmethod
    async def topup(cls, db: AsyncSession, customer_id: UUID, amount: int) -> int:
        account = await cls._account(db, customer_id)
        account.balance += amount
        return account.balance

    @classmethod
    async def charge(
        cls, db: AsyncSession, customer_id: UUID, amount: int, operation_key: str
    ) -> int:
        account = await cls._account(db, customer_id)
        if account.balance < amount:
            raise InsufficientFunds(
                f"balance {account.balance} is below the price {amount}"
            )
        account.balance -= amount
        db.add(
            PaymentsCharge(
                customer_id=customer_id, amount=amount, operation_key=operation_key
            )
        )
        await db.flush()
        return account.balance

    @classmethod
    async def refund(cls, db: AsyncSession, customer_id: UUID, charge_key: str) -> int:
        """Refund the charge made under ``charge_key``; none -> nothing to undo."""
        charge = (
            await db.execute(
                select(PaymentsCharge)
                .where(PaymentsCharge.operation_key == charge_key)
                .with_for_update()
            )
        ).scalar_one_or_none()
        account = await cls._account(db, customer_id)
        if charge is not None and not charge.refunded:
            charge.refunded = True
            account.balance += charge.amount
        return account.balance
