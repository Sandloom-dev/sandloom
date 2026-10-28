"""Payments API: a customer's own balance and top-ups.

``module_router()`` authenticates every route and binds the tenant (the
module's tables are ``TenantScoped``).
"""

from sandloom.sdk import CurrentUser, module_router
from sandloom.sdk.web import DbSession  # sandloom: advanced db() lands with H1

from ..schemas import BalanceOut, TopupIn
from ..services import Ledger

router = module_router()


@router.post(
    "/topup",
    response_model=BalanceOut,
)
async def topup(data: TopupIn, db: DbSession, user: CurrentUser) -> BalanceOut:
    """Add to the caller's prepaid balance (demo: no PSP behind it)."""
    balance = await Ledger.topup(db, user.user_id, data.amount)
    return BalanceOut(customer_id=user.user_id, balance=balance)


@router.get(
    "/balance",
    response_model=BalanceOut,
)
async def balance(db: DbSession, user: CurrentUser) -> BalanceOut:
    return BalanceOut(
        customer_id=user.user_id, balance=await Ledger.balance(db, user.user_id)
    )
