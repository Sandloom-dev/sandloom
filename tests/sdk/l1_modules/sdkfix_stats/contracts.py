"""``sdkfix_stats.quota.v1``: a read and two commands (Appendix E shape)."""

from __future__ import annotations

from sandloom.sdk import command, contract

LIMIT = 3


@contract("sdkfix_stats.quota.v1")
class Quota:
    async def may_create_note(self) -> bool:
        return True

    @command(idempotency_key="operation_key")
    async def reserve(self, amount: int, operation_key: str) -> int:
        return amount

    @command(idempotency_key="operation_key")
    async def release(self, amount: int, operation_key: str) -> int:
        return -amount
