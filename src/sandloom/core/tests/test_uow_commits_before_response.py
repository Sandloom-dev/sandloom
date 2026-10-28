"""The request Unit of Work commits BEFORE the response is sent.

FastAPI >= 0.118 runs a yield dependency's exit code only after the response
went out unless the dependency is declared with ``scope="function"``. For the
UoW that would break read-your-writes across requests: a client acting on a
``200`` (promote a member, then use the new role; change the password, then
expect old tokens to be revoked) could race the commit and its on-commit hooks.
Pins both the behaviour and that every framework ``Depends(get_db ...)``
declares the function scope (FastAPI caches dependencies per scope, so a
mixed declaration would open two UoWs in one request).
"""

from __future__ import annotations

import ast
import asyncio
import typing
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from sandloom.core import di

SRC = Path(__file__).resolve().parents[2]  # src/sandloom


class _DummyCore:
    def __init__(self, provider: Any) -> None:
        self.get_db = provider


def _scope_of(marker: Any) -> Any:
    return getattr(marker, "scope", None)


def test_dbsession_and_database_markers_use_function_scope() -> None:
    (marker,) = typing.get_args(di.DbSession)[1:]
    assert _scope_of(marker) == "function"
    assert _scope_of(di.Database("analytics")) == "function"


def test_every_framework_get_db_dependency_declares_function_scope() -> None:
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", getattr(node.func, "attr", None))
                == "Depends"
                and node.args
            ):
                continue
            target = node.args[0]
            name = getattr(target, "id", None) or getattr(target, "attr", None)
            if isinstance(target, ast.Call):
                name = getattr(target.func, "id", None)
            if name not in {"get_db", "make_db_dependency"}:
                continue
            scope = next((k.value for k in node.keywords if k.arg == "scope"), None)
            literal = isinstance(scope, ast.Constant) and scope.value == "function"
            named = isinstance(scope, ast.Name) and scope.id == "_UOW_DEPENDENCY_SCOPE"
            if not (literal or named):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert not offenders, offenders


def test_db_session_dependency_exits_before_the_response_starts() -> None:
    events: list[str] = []

    async def provider() -> AsyncGenerator[object, None]:  # pragma: no cover
        yield object()

    async def override() -> AsyncGenerator[object, None]:
        try:
            yield object()
        finally:
            events.append("uow-exit")  # where the real UoW commits

    app = FastAPI()
    app.state.core = _DummyCore(provider)
    app.dependency_overrides[provider] = override

    @app.post("/write")
    async def write(db: di.DbSession) -> dict[str, bool]:
        events.append("endpoint")
        return {"ok": True}

    async def run() -> None:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/write",
            "raw_path": b"/write",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"t")],
            "client": ("127.0.0.1", 1),
            "server": ("t", 80),
        }

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                events.append(f"response-start:{message['status']}")

        await app(scope, receive, send)

    asyncio.run(run())
    assert events == ["endpoint", "uow-exit", "response-start:200"]
