"""``payments.charges.v1`` — the command contract other modules (and sagas) call.

Runs in this module's own Unit of Work on its own database, identically for
a local call and over internal RPC. Each method is a ``@command`` naming its
``operation_key``: a repeat with the same key replays the stored result
instead of running again. ``sandloom test`` generates the typed stub callers
import (``academy.shared.contracts.payments_charges_v1``).
"""

from uuid import UUID

from sandloom.sdk import command, contract, db

from .services import Ledger


@contract("payments.charges.v1")
class Charges:
    @command(idempotency_key="operation_key", risk="money")
    async def charge(self, customer_id: UUID, amount: int, operation_key: str) -> int:
        """Charge ``amount``; the new balance. 402 when the balance is short."""
        return await Ledger.charge(db(), customer_id, amount, operation_key)

    @command(idempotency_key="operation_key", risk="money")
    async def refund(
        self, customer_id: UUID, charge_key: str, operation_key: str
    ) -> int:
        """Undo the charge made under ``charge_key`` (none: nothing to undo)."""
        return await Ledger.refund(db(), customer_id, charge_key)

    @command(idempotency_key="operation_key", risk="money")
    async def topup(self, customer_id: UUID, amount: int, operation_key: str) -> int:
        """Add ``amount`` to the balance; the new balance."""
        return await Ledger.topup(db(), customer_id, amount)
