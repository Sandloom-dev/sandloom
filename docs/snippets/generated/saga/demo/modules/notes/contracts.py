"""Contracts the notes module provides."""

from sandloom.sdk import command, contract

from .schemas import OnboardingLedgerStatus


@contract("notes.onboarding_ledger.v1")
class OnboardingLedger:
    """Contract ``notes.onboarding_ledger.v1``: public ``async def`` methods are reads,
    ``@command`` marks a command (deduplicated by its key, a saga step)."""

    async def status(self) -> OnboardingLedgerStatus:
        return OnboardingLedgerStatus(ready=True)

    @command(idempotency_key="operation_key")
    async def record(self, amount: int, operation_key: str) -> int:
        return amount
