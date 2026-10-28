"""The write store of a route (spec §6.1; SANDLOOM-B009 / B013 / B014)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Depends, FastAPI

from sandloom.core import di
from sandloom.core.http.route_runtime import (
    store_capability_problems,
    validate_store_capabilities,
)
from sandloom.core.route_policy import (
    StoreCapabilityError,
    WriteStoreError,
    validate_write_stores,
    write_store_problems,
)
from sandloom.core.sql_alchemy import write_admission
from sandloom.core.unit_of_work.request import WriteAdmission
from sandloom.core.unit_of_work.scope import _DB_NAME_KEY
from sandloom.shared.authz import current_context

USER = [Depends(current_context)]


def _app(*routes: tuple[str, str, list[Any], Any]) -> FastAPI:
    app = FastAPI()
    for path, method, deps, endpoint in routes:
        app.add_api_route(path, endpoint, methods=[method], dependencies=deps)
    return app


async def _two(db: di.DbSession, other: Any = di.Database("analytics")) -> None:
    return None


async def _one(db: di.DbSession) -> None:
    return None


def test_several_writable_sessions_need_a_write_store() -> None:
    app = _app(
        ("/ambiguous", "POST", USER, _two),
        ("/named", "POST", [*USER, di.writes("analytics")], _two),
        ("/read", "GET", USER, _two),
        ("/single", "POST", USER, _one),
        ("/wrong", "POST", [*USER, di.writes("archive")], _one),
        (
            "/waived",
            "POST",
            [*USER, di.allow_multi_db_write(secondaries=("analytics",), reason="r")],
            _two,
        ),
        (
            "/bad-waiver",
            "POST",
            [
                *USER,
                di.writes("default"),
                di.allow_multi_db_write(secondaries=("default",), reason="r"),
            ],
            _two,
        ),
    )
    problems = write_store_problems(app.routes)
    joined = "\n".join(problems)
    assert "POST /ambiguous: several writable sessions (default, analytics)" in joined
    assert "POST /wrong: writes('archive') names a database" in joined
    assert "POST /bad-waiver: allow_multi_db_write secondary 'default'" in joined
    for fine in ("/named", "/read", "/single", "/waived"):
        assert fine + ":" not in joined, fine
    assert len(problems) == 3
    with pytest.raises(WriteStoreError, match="SANDLOOM-B009"):
        validate_write_stores(app.routes)


def test_a_money_effect_in_an_opted_out_secondary_is_refused() -> None:
    from sandloom.core.sql_alchemy.money_ledger import MONEY_TABLES

    MONEY_TABLES.setdefault("wsa_b014_ledger", "operation_key")
    context = SimpleNamespace(
        compiled=SimpleNamespace(
            statement=SimpleNamespace(table=SimpleNamespace(name="wsa_b014_ledger"))
        )
    )
    adm = WriteAdmission(write_store="default", secondaries=("analytics",))
    secondary = SimpleNamespace(info={_DB_NAME_KEY: "analytics"})
    with pytest.raises(
        write_admission.MoneyEffectOutsideWriteStoreError, match="SANDLOOM-B014"
    ):
        write_admission._note_ledger_insert(context, secondary, adm)
    store = SimpleNamespace(info={_DB_NAME_KEY: "default"})
    write_admission._note_ledger_insert(context, store, adm)  # the write store: fine


_FULL = frozenset({"transactions", "write_admission", "audit", "http_idempotency"})


def _features(**over: frozenset[str] | None) -> Any:
    table: dict[str, frozenset[str] | None] = {"default": _FULL, **over}
    return lambda name: table.get(name, _FULL)


def test_a_write_store_without_a_default_feature_is_refused_at_boot() -> None:
    """SANDLOOM-B010 (spec §6.1): a sensitive write needs ``audit`` on its write
    store (the mandatory audit row); an explicit durable guard needs
    ``http_idempotency``; a read or a store that has them passes."""
    app = _app(
        ("/sensitive", "POST", [*USER, di.sensitive("security")], _one),
        (
            "/durable",
            "POST",
            [*USER, Depends(di.idempotency_guard(scope="wsa.b010", durable=True))],
            _one,
        ),
        ("/plain", "POST", USER, _one),
        ("/read", "GET", [*USER, di.sensitive("security")], _one),
    )
    bare = frozenset({"transactions", "write_admission"})
    problems = store_capability_problems(app.routes, _features(default=bare))
    joined = "\n".join(problems)
    assert "POST /sensitive: database 'default' lacks the 'audit' feature" in joined
    assert "POST /durable: database 'default' lacks the 'http_idempotency'" in joined
    assert "/plain" not in joined and "/read" not in joined
    assert all("SANDLOOM-B010" in p for p in problems) and len(problems) == 2
    assert store_capability_problems(app.routes, _features()) == []
    registry = SimpleNamespace(advertised_features=lambda _cap, _name: bare)
    with pytest.raises(StoreCapabilityError, match="SANDLOOM-B010"):
        validate_store_capabilities(app.routes, registry)


def test_a_non_transactional_store_in_a_write_route_is_refused() -> None:
    """SANDLOOM-B011: a writable session on a store without ``transactions``
    cannot commit with the request unit, unless it is the reviewed opt-out
    (an ``allow_multi_db_write`` secondary); unconfigured stores are left to
    the other checks."""
    app = _app(
        ("/mixed", "POST", [*USER, di.writes("default")], _two),
        (
            "/waived",
            "POST",
            [*USER, di.allow_multi_db_write(secondaries=("analytics",), reason="r")],
            _two,
        ),
        ("/reads", "GET", USER, _two),
    )
    docs = frozenset({"conditional_write"})
    problems = store_capability_problems(app.routes, _features(analytics=docs))
    b011 = [p for p in problems if "SANDLOOM-B011" in p]
    assert len(b011) == 1, problems
    assert b011[0].startswith("POST /mixed: database 'analytics' is not transactional")
    assert store_capability_problems(app.routes, _features(analytics=None)) == []


def test_a_database_hosting_request_units_needs_write_admission() -> None:
    """SANDLOOM-B010 (the relational driver contract): every database that hosts
    request units — the default one, a route's Database("x") — must advertise
    ``write_admission`` (one writer per request, write detection, closed-unit
    refusal, the lazy unit); the SQLAlchemy driver does."""
    app = _app(("/two", "GET", USER, _two))
    without = frozenset({"transactions"})
    problems = store_capability_problems(app.routes, _features(analytics=without))
    assert problems == [
        p for p in problems if p.startswith("database 'analytics' hosts request units")
    ]
    assert len(problems) == 1 and "write_admission" in problems[0]
    assert store_capability_problems(app.routes, _features()) == []
    from sandloom.core.drivers.sqlalchemy_database import SQLALCHEMY_DB_FEATURES

    assert "write_admission" in SQLALCHEMY_DB_FEATURES
