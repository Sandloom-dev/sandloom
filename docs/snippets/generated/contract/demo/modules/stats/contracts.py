"""Contracts the stats module provides."""

from sandloom.sdk import command, contract

from .schemas import QuotaStatus


@contract("stats.quota.v1")
class Quota:
    """Contract ``stats.quota.v1``: public ``async def`` methods are reads,
    ``@command`` marks a command (deduplicated by its key, a saga step)."""

    async def status(self) -> QuotaStatus:
        return QuotaStatus(ready=True)

    @command(idempotency_key="operation_key")
    async def record(self, amount: int, operation_key: str) -> int:
        return amount
