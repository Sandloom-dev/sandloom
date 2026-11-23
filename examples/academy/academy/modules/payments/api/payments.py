"""Payments API: a customer's own balance and top-ups.

``module_router()`` authenticates every route and binds the tenant (the
module's tables are ``TenantScoped``).
"""

from sandloom.sdk import CurrentUser, module_router

from ..schemas import BalanceOut, TopupIn
from ..services import Ledger

router = module_router()


@router.post(
    "/topup",
    response_model=BalanceOut,
)
async def topup(data: TopupIn, user: CurrentUser) -> BalanceOut:
    """Add to the caller's prepaid balance (demo: no PSP behind it)."""
    balance = await Ledger.topup(user.user_id, data.amount)
    return BalanceOut(customer_id=user.user_id, balance=balance)


@router.get(
    "/balance",
    response_model=BalanceOut,
)
async def balance(user: CurrentUser) -> BalanceOut:
    return BalanceOut(
        customer_id=user.user_id, balance=await Ledger.balance(user.user_id)
    )
