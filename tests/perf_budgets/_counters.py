"""Deterministic round-trip counters for the performance-budget tests."""

from __future__ import annotations

from typing import Any

import pytest


class RedisCommandCounter:
    """Counts the commands a redis.asyncio client sends (one per round trip;
    a Lua script is one ``EVALSHA``)."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.enabled = False

    def wrap(self, client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        original = client.execute_command

        async def _counting(*args: Any, **kwargs: Any) -> Any:
            if self.enabled:
                self.commands.append(str(args[0]).upper())
            return await original(*args, **kwargs)

        monkeypatch.setattr(client, "execute_command", _counting)

    def reset(self) -> None:
        self.commands.clear()

    @property
    def count(self) -> int:
        return len(self.commands)


class SqlStatementCounter:
    """Counts SQL statements (each a Postgres round trip at least)."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.enabled = False

    def __call__(self, _conn: Any, _cursor: Any, statement: str, *_a: Any) -> None:
        if self.enabled:
            self.statements.append(" ".join(statement.split())[:120])

    @property
    def count(self) -> int:
        return len(self.statements)
