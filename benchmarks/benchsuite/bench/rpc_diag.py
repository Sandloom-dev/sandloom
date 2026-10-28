"""RPC cost-decomposition contract — DIAGNOSTIC ONLY, NEVER PRODUCTION.

Registered as ``bench.rpcdiag.v1`` by the ``bench`` module only with
``BENCH_RPC_DIAG=1`` (``benchsuite.bench._setup``). Every method has the SAME
wire signature as ``tenancy.membership.v1.get_role`` — ``(organization_id:
UUID, user_id: UUID) -> str | None`` — so the codec / signing / transport work
is identical on every rung and only the provider-side body differs:

==========  =================================================================
method      provider body (what the rung adds on top of the previous one)
==========  =================================================================
noop        planned read (``planned=True``): NO Unit of Work, no DB — the
            floor of the contract runtime (R0 local, R1 over RPC)
noop_ids    ``noop`` returning ``list[UUID]`` (copied per call; the hot-path
            stand's get_service / memo rungs, ``ladder/hot_paths.py``)
uow_noop    plain read contract: the provider's own read UoW is opened and
            rolled back, ``contract_session()`` is touched, NO statement (R2)
uow_select  ``uow_noop`` + ``SELECT 1`` (connection checkout, BEGIN, ROLLBACK)
role_uow    the membership-role query of ``tenancy.membership.v1.get_role``
            as raw SQL on ``contract_session()`` (the pre-planner UoW form,
            R3u — compare with the real planned contract, R3 / R3o)
touch       COMMAND (``operation_key``): framework dedupe in the provider's
            durable command log (claim + complete in its committed UoW), no
            body statement (R4)
==========  =================================================================

``raw_noop`` is the undecorated body (R0raw): a plain coroutine call, the
floor below the contract runtime.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from sandloom.core.module_api import contract_method, contract_session

SERVICE = "bench.rpcdiag.v1"

#: The SQL the ``tenancy.member.role_in_active_org`` JoinRead plans (same
#: tables, same predicates, same unique-key lookup).
ROLE_SQL = text(
    "SELECT m.role FROM tenancy_members AS m "
    "JOIN tenancy_organizations AS o ON o.id = m.organization_id "
    "WHERE m.organization_id = :org AND m.user_id = :user AND o.is_active"
)


async def raw_noop(organization_id: UUID, user_id: UUID) -> str | None:
    """R0raw: the body without any contract machinery."""
    return "owner"


class RpcDiagContract:
    """``bench.rpcdiag.v1`` — see the module docstring for each rung."""

    @classmethod
    @contract_method(planned=True)
    async def noop(cls, organization_id: UUID, user_id: UUID) -> str | None:
        return "owner"

    @classmethod
    @contract_method(planned=True)
    async def noop_ids(cls, organization_id: UUID, user_id: UUID) -> list[UUID]:
        """``noop`` returning a mutable result (the hot-path stand's memo rungs:
        an in-process caller gets its own copy, like over RPC)."""
        return [organization_id, user_id]

    @classmethod
    @contract_method
    async def uow_noop(cls, organization_id: UUID, user_id: UUID) -> str | None:
        contract_session()  # the UoW is open; nothing is sent
        return "owner"

    @classmethod
    @contract_method
    async def uow_select(cls, organization_id: UUID, user_id: UUID) -> str | None:
        await contract_session().execute(text("SELECT 1"))
        return "owner"

    @classmethod
    @contract_method
    async def role_uow(cls, organization_id: UUID, user_id: UUID) -> str | None:
        row = (
            await contract_session().execute(
                ROLE_SQL, {"org": organization_id, "user": user_id}
            )
        ).first()
        return None if row is None else str(row[0])

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def touch(
        cls, organization_id: UUID, user_id: UUID, operation_key: str
    ) -> str | None:
        contract_session()
        return "owner"


__all__ = ["ROLE_SQL", "SERVICE", "RpcDiagContract", "raw_noop"]
