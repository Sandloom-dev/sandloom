"""Architecture rule: registered contract methods never take a DB session.

A sync service contract has identical semantics in a monolith and over
internal RPC only if the PROVIDER owns the transaction: every call runs in the
provider's own Unit of Work on its own database (``@contract_method`` +
``contract_session()``). A contract method that declares an ``AsyncSession`` /
``DbSession`` parameter (or one named ``db`` / ``session``) would run in the
caller's transaction locally and in a fresh one remotely — exactly the split
behaviour this rule forbids.

The check is runtime-introspective: it loads the batteries monolith (every
bundled module registered) and inspects the PUBLIC callables of every
registered contract — including the auto-registered ``<module>.claims.v1``.
"""

from __future__ import annotations

import inspect
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.core.module_api import contract_method
from sandloom.core.module_api._spec import session_parameters


def _public_methods(impl: Any) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for name in dir(impl):
        if name.startswith("_"):
            continue
        attr = getattr(impl, name)
        if callable(attr) and not inspect.isclass(attr):
            out.append((name, attr))
    return out


def find_session_parameters(impl: Any) -> list[str]:
    """``"method(param)"`` for every public method declaring a session."""
    return [
        f"{name}({', '.join(params)})"
        for name, method in _public_methods(impl)
        if (params := session_parameters(method))
    ]


def test_registered_contracts_take_no_session() -> None:
    from sandloom.main import core

    offences: dict[str, list[str]] = {}
    for service_name in core.service_registry.list():
        impl = core.service_registry.get(service_name)
        found = find_session_parameters(impl)
        if found:
            offences[service_name] = found
    assert not offences, (
        "Registered contract methods must not take a database session — the "
        "provider opens its own Unit of Work (decorate with @contract_method "
        "and read contract_session()):\n"
        + "\n".join(f"  {svc}: {', '.join(m)}" for svc, m in offences.items())
    )


def test_analyzer_detects_session_parameters() -> None:
    """Self-test: annotated and name-only session parameters are both flagged."""

    class Leaky:
        @classmethod
        async def by_type(cls, user_id: int, conn: AsyncSession) -> int:
            return user_id

        @classmethod
        async def by_name(cls, user_id: int, db: Any) -> int:
            return user_id

        @classmethod
        @contract_method
        async def clean(cls, user_id: int) -> int:
            return user_id

    found = find_session_parameters(Leaky)
    assert "by_type(conn)" in found
    assert "by_name(db)" in found
    assert not any(f.startswith("clean") for f in found)
