"""The contract the fixture module provides."""

from __future__ import annotations

from sandloom.sdk import command, contract


@contract("sdkfix_notes.lookup.v1")
class Lookup:
    async def title_length(self, title: str) -> int:
        return len(title)

    @command(idempotency_key="operation_key")
    async def archive(self, note_id: str, operation_key: str) -> bool:
        return bool(note_id and operation_key)
