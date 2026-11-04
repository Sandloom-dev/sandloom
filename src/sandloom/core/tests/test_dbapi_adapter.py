"""The DBAPI adapter seam (spec infra-agnostic, slice 7b).

The asyncpg specifics moved out of ``core/sql_alchemy`` into
``core/drivers/dbapi_asyncpg.py``; the hooks ask the adapter of the engine's
DBAPI. On PostgreSQL + asyncpg every text, connect argument and
classification is pinned against the code before this slice (its literals
and functions, kept here as the oracle). A DBAPI without an adapter takes
the plain path; a database with neither read-only transactions nor a client
read-only policy refuses ``DB_READ_ONLY_SESSIONS`` (SANDLOOM-B031).
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import sqlite
from sqlalchemy.ext.asyncio import create_async_engine

from sandloom.core import app_factory, error_codes
from sandloom.core.drivers import dbapi_asyncpg
from sandloom.core.drivers.dbapi_asyncpg import ASYNCPG, begin_texts
from sandloom.core.drivers.sql_dialects import POSTGRESQL
from sandloom.core.facade._claims import _in_failed_transaction as claims_failed
from sandloom.core.module_api._read_scope import (
    _in_failed_transaction as scope_failed,
)
from sandloom.core.sql_alchemy import dbapi
from sandloom.core.sql_alchemy.dead_connection import (
    install_dead_connection_handling,
    is_dead_connection_error,
)
from sandloom.core.sql_alchemy.planner_adapter import SQLAlchemyExecutionAdapter
from sandloom.core.sql_alchemy.read_only_policy import (
    ReadOnlySessionsUnavailable,
    install_read_executor,
    read_only_sessions_problem,
)
from sandloom.core.sql_alchemy.reconnect import ReconnectBackoff


@pytest.fixture(autouse=True)
def _registered() -> Iterator[None]:
    """What the SQLAlchemy driver does before it builds an engine."""
    saved = dict(dbapi._adapters)
    dbapi.register_dbapi_adapter(ASYNCPG)
    yield
    dbapi._adapters.clear()
    dbapi._adapters.update(saved)


# --- the oracle: the code before slice 7b ------------------------------------

_OLD_ISOLATION_SQL = {
    "read_committed": " ISOLATION LEVEL READ COMMITTED",
    "read_uncommitted": " ISOLATION LEVEL READ UNCOMMITTED",
    "repeatable_read": " ISOLATION LEVEL REPEATABLE READ",
    "serializable": " ISOLATION LEVEL SERIALIZABLE",
}


def _old_begin_sql(isolation: str | None, readonly: bool, deferrable: bool) -> str:
    """driver_compat.begin_sql before slice 7b."""
    sql = "BEGIN" + _OLD_ISOLATION_SQL.get(isolation or "", "")
    if readonly:
        sql += " READ ONLY"
    if deferrable:
        sql += " DEFERRABLE"
    return sql


def _old_server_settings(
    url: str, statement: int, idle: int, extra: dict[str, str] | None
) -> dict[str, str]:
    """sqlalchemy_database._server_settings before slice 7b."""
    if not url.startswith("postgresql+asyncpg"):
        return {}
    values: dict[str, str] = {}
    if statement:
        values["statement_timeout"] = str(statement)
    if idle:
        values["idle_in_transaction_session_timeout"] = str(idle)
    values.update(extra or {})
    return values


def _old_failed_transaction(exc: BaseException) -> bool:
    """facade._claims / module_api._read_scope ``_in_failed_transaction``
    before slice 7b."""
    seen: set[int] = set()
    stack: list[Any] = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        for attr in ("sqlstate", "pgcode"):
            if getattr(current, attr, None) == "25P02":
                return True
        stack.extend(
            (
                getattr(current, "orig", None),
                getattr(current, "__cause__", None),
                getattr(current, "__context__", None),
            )
        )
    return False


# --- the registry --------------------------------------------------------------


def test_an_unknown_dbapi_gets_the_plain_path() -> None:
    plain = dbapi.dbapi_adapter("aiosqlite")
    assert plain is dbapi.PLAIN_DBAPI
    assert plain.capabilities == frozenset()
    assert dbapi.dbapi_adapter_of(sqlite.dialect()) is dbapi.PLAIN_DBAPI
    error = RuntimeError("connection is closed")
    assert not plain.never_sent(error)
    assert not plain.recoverable(object(), error)
    assert not plain.recovery_supported()
    assert not plain.refuses_connections(error)
    assert plain.merged_begin(POSTGRESQL.row_security(), POSTGRESQL) is None
    assert plain.connect_args(connect_timeout=5, server_settings={"a": "b"}) == {}
    # Only a socket error counts as a lost connection there.
    assert is_dead_connection_error(ConnectionResetError(), plain)
    assert not is_dead_connection_error(error, plain)


def test_the_adapter_is_chosen_by_the_urls_dbapi() -> None:
    assert dbapi.dbapi_of_url("postgresql+asyncpg://u@h/db") == "asyncpg"
    assert dbapi.dbapi_of_url("sqlite+aiosqlite:///x.db") == "aiosqlite"
    assert dbapi.dbapi_of_url("postgresql://u@h/db") == ""
    engine = create_async_engine("postgresql+asyncpg://u:p@localhost:1/x")
    try:
        assert dbapi.dbapi_adapter_of(engine) is ASYNCPG
        assert dbapi.dbapi_adapter_of(engine.sync_engine) is ASYNCPG
    finally:
        engine.sync_engine.dispose()


@pytest.mark.parametrize(
    ("adapter", "message"),
    [
        (object(), "is not"),
        (dbapi.DbapiAdapter(), "names no DBAPI"),
        (
            type(
                "Odd", (dbapi.DbapiAdapter,), {"name": "odd", "capabilities": {"x"}}
            )(),
            "unknown capabilities",
        ),
    ],
)
def test_registration_refuses_an_invalid_adapter(adapter: Any, message: str) -> None:
    with pytest.raises(dbapi.InvalidDbapiAdapter, match=message):
        dbapi.register_dbapi_adapter(adapter)


def test_asyncpg_supports_every_capability() -> None:
    assert ASYNCPG.capabilities == dbapi.DBAPI_CAPABILITIES


# --- byte identity on PostgreSQL + asyncpg ---------------------------------------


@pytest.mark.parametrize(
    ("isolation", "readonly", "deferrable"),
    list(itertools.product([None, *_OLD_ISOLATION_SQL], [False, True], [False, True])),
)
def test_merged_begin_texts_are_the_old_ones(
    isolation: str | None, readonly: bool, deferrable: bool
) -> None:
    texts = begin_texts(POSTGRESQL)
    assert texts[(isolation, readonly, deferrable)] == _old_begin_sql(
        isolation, readonly, deferrable
    )


def test_the_kernels_units_take_the_renderers_begin_text() -> None:
    """One BEGIN source: the units the kernel opens are the renderer's text,
    the one the planner sends; any other state is asyncpg's own."""
    renderer = SimpleNamespace(
        begin_statement=lambda *, write, read_only, snapshot: (
            f"RENDERED {write} {read_only} {snapshot}"
        )
    )
    texts = begin_texts(renderer)  # type: ignore[arg-type]
    assert texts[("read_committed", True, False)] == "RENDERED False True False"
    assert texts[("repeatable_read", True, False)] == "RENDERED False True True"
    assert texts[("repeatable_read", False, False)] == "RENDERED True False True"
    assert texts[(None, False, False)] == "RENDERED True False False"
    assert texts[("serializable", False, False)] == _old_begin_sql(
        "serializable", False, False
    )
    assert texts[(None, True, False)] == "BEGIN READ ONLY"


@pytest.mark.parametrize("statement", [0, 5000])
@pytest.mark.parametrize("idle", [0, 60000])
@pytest.mark.parametrize("extra", [None, {"lock_timeout": "250"}])
@pytest.mark.parametrize(
    "url", ["postgresql+asyncpg://u@h/db", "sqlite+aiosqlite:///x.db"]
)
def test_connect_arguments_are_the_old_ones(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    statement: int,
    idle: int,
    extra: dict[str, str] | None,
) -> None:
    from sandloom.core.drivers import sqlalchemy_database as driver

    monkeypatch.setattr(driver.settings, "db_statement_timeout_ms", statement)
    monkeypatch.setattr(driver.settings, "db_idle_in_tx_timeout_ms", idle)
    old = _old_server_settings(url, statement, idle, extra)
    assert driver._server_settings(url, extra) == old
    old_args: dict[str, object] = {}
    if url.startswith("postgresql+asyncpg"):
        old_args["timeout"] = 7.5
    if old:
        old_args["server_settings"] = old
    args = driver._dbapi(url).connect_args(
        connect_timeout=7.5, server_settings=driver._server_settings(url, extra)
    )
    assert args == old_args
    assert list(args) == list(old_args)


def test_asyncpg_classification_is_the_old_one() -> None:
    from asyncpg import exceptions as pg

    too_many = pg.TooManyConnectionsError("sorry, too many clients already")
    assert ASYNCPG.refuses_connections(too_many)
    assert not is_dead_connection_error(too_many, ASYNCPG)
    for code in ("08006", "08003", "57P01", "57P02", "57P03"):
        error = RuntimeError(code)
        error.sqlstate = code  # type: ignore[attr-defined]
        assert ASYNCPG.connection_lost(error)
    for code in ("57014", "40001", "25P02", "53300"):
        error = RuntimeError(code)
        error.sqlstate = code  # type: ignore[attr-defined]
        assert not ASYNCPG.connection_lost(error)
    assert ASYNCPG.connection_lost(pg.ConnectionDoesNotExistError("gone"))
    assert not ASYNCPG.connection_lost(pg.InterfaceError("connection is closed"))
    assert ASYNCPG.never_sent(pg.InterfaceError("connection is closed"))


def _with(**attrs: Any) -> BaseException:
    error = RuntimeError("x")
    for name, value in attrs.items():
        setattr(error, name, value)
    return error


def _chains() -> list[BaseException]:
    aborted = _with(sqlstate="25P02")
    wrapped = _with(orig=aborted)
    caused = RuntimeError("translated")
    caused.__cause__ = aborted
    context = RuntimeError("during handling")
    context.__context__ = _with(pgcode="25P02")
    return [
        aborted,
        wrapped,
        caused,
        context,
        _with(sqlstate="40001"),
        _with(sqlstate="25P01"),
        RuntimeError("current transaction is aborted"),
    ]


@pytest.mark.parametrize("error", _chains())
def test_the_aborted_transaction_check_is_the_old_one(error: BaseException) -> None:
    old = _old_failed_transaction(error)
    assert claims_failed(error) is old
    assert scope_failed(error) is old


def test_postgresql_classifies_25p02_as_transaction_aborted() -> None:
    assert POSTGRESQL.classify_error(_with(sqlstate="25P02")) == "transaction_aborted"


# --- the plain path on another DBAPI ---------------------------------------------


def _plain_engine() -> Any:
    dialect = sqlite.dialect()  # pysqlite: no adapter registered
    sync = SimpleNamespace(dialect=dialect)
    return SimpleNamespace(dialect=dialect, sync_engine=sync)


def test_the_hooks_install_nothing_without_an_adapter() -> None:
    engine = _plain_engine()
    install_dead_connection_handling(engine.sync_engine)
    assert not getattr(engine.sync_engine, "_sandloom_dead_connection_installed", 0)
    assert install_read_executor(engine) is False
    assert SQLAlchemyExecutionAdapter(engine)._raw_executor is False


def test_the_planner_raw_executor_is_the_adapters_decision() -> None:
    engine = create_async_engine("postgresql+asyncpg://u:p@localhost:1/x")
    try:
        assert SQLAlchemyExecutionAdapter(engine)._raw_executor is True
        dbapi._adapters.pop("asyncpg")
        assert SQLAlchemyExecutionAdapter(engine)._raw_executor is False
    finally:
        engine.sync_engine.dispose()


def test_reconnect_backoff_defaults_to_the_plain_classification() -> None:
    gate = ReconnectBackoff(base=0.1, cap=1, wait_timeout=1)
    assert gate._dbapi is dbapi.PLAIN_DBAPI


def test_merged_begin_follows_the_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    security = POSTGRESQL.row_security()
    assert security is not None
    monkeypatch.setattr(dbapi_asyncpg, "asyncpg_fast_paths_ok", lambda: True)
    assert ASYNCPG.merged_begin(security, POSTGRESQL) is not None
    monkeypatch.setattr(dbapi_asyncpg, "asyncpg_fast_paths_ok", lambda: False)
    assert ASYNCPG.merged_begin(security, POSTGRESQL) is None


# --- the read-only guard (SANDLOOM-B031) -----------------------------------------


@pytest.mark.parametrize(
    ("url", "refused"),
    [
        ("postgresql+asyncpg://u@h/db", False),  # both guards
        ("postgresql+psycopg://u@h/db", False),  # the read-only transaction
        ("postgresql://u@h/db", False),
        ("sqlite+aiosqlite:///x.db", True),  # neither
    ],
)
def test_read_only_sessions_need_a_guard(url: str, refused: bool) -> None:
    problem = read_only_sessions_problem("default", url)
    assert (problem is not None) is refused
    if refused:
        assert problem is not None and "SANDLOOM-B031" in problem


def test_a_client_policy_alone_is_a_guard() -> None:
    policy = type(
        "Policy",
        (dbapi.DbapiAdapter,),
        {"name": "aiosqlite", "capabilities": frozenset({dbapi.READ_ONLY_POLICY})},
    )()
    dbapi.register_dbapi_adapter(policy)
    assert read_only_sessions_problem("default", "sqlite+aiosqlite:///x.db") is None


def _load_infra(monkeypatch: pytest.MonkeyPatch, url: str | None) -> None:
    options = {} if url is None else {"url": url}
    instances = {"default": SimpleNamespace(options=options)}
    monkeypatch.setattr(
        app_factory, "load_infra_config", lambda _cfg: {"database": instances}
    )


def test_startup_refuses_b031_build_free(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = SimpleNamespace(
        infra={},
        database_url="postgresql+asyncpg://u:p@h/db",
        db_read_only_sessions=True,
    )
    _load_infra(monkeypatch, "sqlite+aiosqlite:///x.db")
    problem = app_factory.read_only_sessions_config_problem(cfg)
    assert problem is not None and "SANDLOOM-B031" in problem
    cfg.db_read_only_sessions = False
    assert app_factory.read_only_sessions_config_problem(cfg) is None
    cfg.db_read_only_sessions = True
    _load_infra(monkeypatch, None)  # DATABASE_URL: PostgreSQL + asyncpg
    assert app_factory.read_only_sessions_config_problem(cfg) is None


def test_b031_is_catalogued() -> None:
    entry = error_codes.lookup("SANDLOOM-B031")
    assert entry is not None and entry.slug == "read-only-sessions-unguarded"
    assert (
        error_codes.EXCEPTION_CODES[ReadOnlySessionsUnavailable.__name__]
        == "SANDLOOM-B031"
    )


def test_citext_is_native_as_a_result_only_through_asyncpg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The raw executor's citext rule is asyncpg's (it decodes citext as
    SQLAlchemy would); a DBAPI without the rule keeps it on the general path."""
    import sqlalchemy as sa
    from sqlalchemy.dialects.postgresql import CITEXT

    import sandloom.core.sql_alchemy.planner_safety as safety

    monkeypatch.setattr(safety, "policy_reason", lambda *_a: None)
    table = sa.Table(
        "probe_citext",
        sa.MetaData(),
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("email", CITEXT, nullable=False),
    )
    columns = [table.c.id, table.c.email]
    engine = create_async_engine("postgresql+asyncpg://u:p@localhost:1/x")
    try:
        assert safety.optimized_reason(engine, [], columns) is None
        keyed = safety.optimized_reason(engine, [], columns, parameters=[table.c.email])
        assert keyed is not None and "email" in keyed
    finally:
        engine.sync_engine.dispose()
    plain = _plain_engine()
    reason = safety.optimized_reason(plain, [], columns)
    assert reason is not None and "email" in reason


# --- review fixes: contract, kernel-only capabilities, order, plain path -------


def _outside(name: str, **attrs: Any) -> dbapi.DbapiAdapter:
    """An adapter class defined by a driver package, not the kernel."""
    cls = type(
        "PackageAdapter",
        (dbapi.DbapiAdapter,),
        {"name": name, "__module__": "sandloom_driver_x.adapter", **attrs},
    )
    return cls()  # type: ignore[no-any-return]


def test_another_contract_major_is_refused() -> None:
    adapter = _outside("x", contract_version=2)
    with pytest.raises(dbapi.InvalidDbapiAdapter, match="SANDLOOM-B025"):
        dbapi.register_dbapi_adapter(adapter)


@pytest.mark.parametrize("capability", sorted(dbapi.KERNEL_ONLY_CAPABILITIES))
def test_a_package_may_not_claim_a_kernel_only_capability(capability: str) -> None:
    adapter = _outside("x", capabilities=frozenset({capability}))
    with pytest.raises(dbapi.InvalidDbapiAdapter, match="only the kernel"):
        dbapi.register_dbapi_adapter(adapter)


def test_a_package_may_claim_the_open_capabilities() -> None:
    open_ = dbapi.DBAPI_CAPABILITIES - dbapi.KERNEL_ONLY_CAPABILITIES
    assert open_ == {dbapi.READ_ONLY_POLICY, dbapi.RECONNECT_BACKOFF}
    adapter = _outside("x", capabilities=frozenset(open_))
    dbapi.register_dbapi_adapter(adapter)
    assert dbapi.dbapi_adapter("x") is adapter


def test_the_shipped_adapter_never_replaces_a_packages() -> None:
    dbapi._adapters.clear()
    own = _outside("asyncpg", capabilities=frozenset({dbapi.READ_ONLY_POLICY}))
    dbapi.register_dbapi_adapter(own)
    assert dbapi.register_shipped_dbapi_adapter(ASYNCPG) is False
    assert dbapi.dbapi_adapter("asyncpg") is own
    dbapi._adapters.clear()
    assert dbapi.register_shipped_dbapi_adapter(ASYNCPG) is True
    assert dbapi.dbapi_adapter("asyncpg") is ASYNCPG
    dbapi.register_dbapi_adapter(own)  # a package registering later wins
    assert dbapi.dbapi_adapter("asyncpg") is own


@pytest.mark.parametrize("code", ["57P01", "57P02", "57P03", "08006", "08003"])
def test_the_plain_path_classifies_a_lost_connection_by_the_renderer(
    code: str,
) -> None:
    """postgresql+psycopg (no adapter): a lost connection is still a 503 on
    the planner's read paths, never retried."""
    psycopg = dbapi.dbapi_adapter_of(SimpleNamespace(driver="psycopg"))
    assert psycopg is dbapi.PLAIN_DBAPI
    driver_error = _with(sqlstate=code)
    wrapped = RuntimeError("(psycopg.OperationalError) terminating connection")
    wrapped.__cause__ = driver_error
    assert is_dead_connection_error(driver_error, psycopg)
    assert is_dead_connection_error(wrapped, psycopg)
    assert not psycopg.never_sent(wrapped)  # classified, not recovered
    assert not is_dead_connection_error(_with(sqlstate="40001"), psycopg)


def test_a_raising_renderer_does_not_mask_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.sql_alchemy import portable

    class Broken:
        def classify_error(self, exc: BaseException) -> str:
            raise RuntimeError("renderer bug")

    renderers = {"broken": Broken(), "postgresql": POSTGRESQL}
    monkeypatch.setattr(portable, "_renderers", lambda: renderers)
    assert portable.is_error_kind(_with(sqlstate="25P02"), "transaction_aborted")
    assert not portable.is_error_kind(RuntimeError("x"), "transaction_aborted")


def test_the_asyncpg_error_classes_are_looked_up_once() -> None:
    first = dbapi_asyncpg._driver_errors()
    assert dbapi_asyncpg._driver_errors() is first
