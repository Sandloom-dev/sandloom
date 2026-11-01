"""Driver-internals probes behind the DB fast paths (no database needed).

The merged BEGIN and the autocommit mark-delivered reach private
asyncpg / SQLAlchemy state. These tests pin: the probes pass on the installed
(verified) versions; the installed versions are inside the verified range and
that range is what ``pyproject.toml`` pins; a changed internal is DETECTED
(name and behaviour) and turns into one warning + a metric + the plain path.
The database-level proof that the plain paths are taken and correct lives in
``tests/perf_budgets/test_rls_merged_begin.py`` and
``test_relay_budgets.py`` (``*_probe_fails_*``).
"""

from __future__ import annotations

import logging
import re
import tomllib
from collections.abc import Iterator
from importlib import metadata
from pathlib import Path
from typing import Any

import pytest
from packaging.version import Version
from sqlalchemy.dialects.postgresql import asyncpg as sa_asyncpg
from sqlalchemy.ext.asyncio import create_async_engine

from sandloom.core.observability.metrics import db_driver_fast_path_disabled_total
from sandloom.core.sql_alchemy import driver_compat, read_only_policy, tenant_rls
from sandloom.core.sql_alchemy.driver_compat import (
    ASYNCPG_TRANSACTION,
    POOL_FAST_CHECKOUT,
    SESSION_CONNECTIONS,
    VERIFIED_DRIVER_RANGES,
)

BACKEND = Path(__file__).resolve().parents[4]


@pytest.fixture(autouse=True)
def _fresh_probes() -> Iterator[None]:
    driver_compat.reset_driver_probes()
    yield
    driver_compat.reset_driver_probes()


def _disabled(probe: str) -> float:
    return float(db_driver_fast_path_disabled_total.labels(probe=probe)._value.get())


# --- the installed drivers -------------------------------------------------


@pytest.mark.parametrize(
    "name", [ASYNCPG_TRANSACTION, SESSION_CONNECTIONS, POOL_FAST_CHECKOUT]
)
def test_probes_pass_on_the_installed_drivers(name: str) -> None:
    result = driver_compat.probe(name)
    assert result.ok, result.problems


@pytest.mark.parametrize("dist", sorted(VERIFIED_DRIVER_RANGES))
def test_installed_driver_is_inside_the_verified_range(dist: str) -> None:
    low, high = VERIFIED_DRIVER_RANGES[dist]
    installed = Version(metadata.version(dist))
    assert Version(low) <= installed < Version(high), (
        f"{dist} {installed} is outside the range the DB fast paths were "
        f"verified for [{low}, {high}); follow docs/operate/operator-reference.md "
        "'Upgrading asyncpg / SQLAlchemy' and then widen VERIFIED_DRIVER_RANGES "
        "and pyproject.toml together"
    )


@pytest.mark.parametrize(
    "pyproject",
    [
        BACKEND / "pyproject.toml",
        BACKEND / "packages/sandloom/pyproject.toml",
    ],
    ids=["backend", "sandloom"],
)
def test_pyproject_pins_exactly_the_verified_range(pyproject: Path) -> None:
    if not pyproject.exists():
        pytest.skip("source tree not available")
    project = tomllib.loads(pyproject.read_text())["project"]
    deps = project["dependencies"]
    if pyproject == BACKEND / "pyproject.toml":
        # The contributor environment installs the distribution that owns drivers.
        assert any(d.startswith("sandloom[") for d in deps), deps
        kernel = BACKEND / "packages/sandloom/pyproject.toml"
        deps = tomllib.loads(kernel.read_text())["project"]["dependencies"]
    for dist, (low, high) in VERIFIED_DRIVER_RANGES.items():
        spec = next(d for d in deps if re.match(rf"{dist}(\[|>|=|<|$)", d))
        assert spec.endswith(f">={low},<{high}"), (pyproject, spec)


# --- detection ----------------------------------------------------------------


def test_a_missing_adapter_attribute_is_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Renamed:  # the adapter after an upgrade that renamed its mutex
        _transaction = None
        _connection = None
        isolation_level = readonly = deferrable = None

        def _handle_exception(self) -> None: ...

        def _start_transaction(self) -> None: ...

    monkeypatch.setattr(sa_asyncpg, "AsyncAdapt_asyncpg_connection", Renamed)
    result = driver_compat.probe(ASYNCPG_TRANSACTION)
    assert not result.ok
    assert any("_execute_mutex is missing" in p for p in result.problems)


def test_a_changed_begin_text_is_detected_by_the_self_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The behavioural self-test runs asyncpg's REAL Transaction.start(): a
    merged BEGIN that would no longer match it is caught."""
    original = driver_compat.begin_sql
    monkeypatch.setattr(
        driver_compat,
        "begin_sql",
        lambda iso, ro, df: original(iso, ro, df).replace(" READ ONLY", " READONLY"),
    )
    result = driver_compat.probe(ASYNCPG_TRANSACTION)
    assert not result.ok
    assert any("BEGIN text changed" in p for p in result.problems)


def test_a_changed_transaction_state_is_detected_by_the_self_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hand-adopting a transaction must leave the state asyncpg's commit /
    rollback accept; a mismatch is caught."""
    monkeypatch.setattr(
        driver_compat, "adopt_started_transaction", lambda adapter, con, tx: None
    )
    result = driver_compat.probe(ASYNCPG_TRANSACTION)
    assert not result.ok


def test_a_failing_probe_warns_once_counts_and_is_cached(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[int] = []

    def missing() -> list[str]:
        calls.append(1)
        return ["SessionTransaction._connections is missing"]

    monkeypatch.setitem(driver_compat.PROBES, SESSION_CONNECTIONS, missing)
    before = _disabled(SESSION_CONNECTIONS)
    with caplog.at_level(logging.WARNING, logger="sandloom.db.driver_compat"):
        for _ in range(3):
            assert driver_compat.session_connections_ok() is False
    assert calls == [1]
    assert _disabled(SESSION_CONNECTIONS) == before + 1
    warnings = [
        r for r in caplog.records if "driver internals changed" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "_connections is missing" in warnings[0].getMessage()


def test_a_crashing_probe_disables_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> list[str]:
        raise RuntimeError("boom")

    monkeypatch.setitem(driver_compat.PROBES, ASYNCPG_TRANSACTION, boom)
    assert driver_compat.asyncpg_fast_paths_ok() is False


# --- the fast paths consult the probe --------------------------------------


def _missing(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setitem(driver_compat.PROBES, name, lambda: ["simulated: missing"])


async def test_merged_begin_is_not_installed_when_the_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _missing(monkeypatch, ASYNCPG_TRANSACTION)
    seen: list[bool] = []
    original = tenant_rls._wrap_execute

    def spy(fn: Any, merged_begin: bool = False) -> Any:
        seen.append(merged_begin)
        return original(fn, merged_begin)

    monkeypatch.setattr(tenant_rls, "_wrap_execute", spy)
    engine = create_async_engine("postgresql+asyncpg://u:p@localhost:1/x")
    try:
        tenant_rls.install_tenant_rls(engine, merged_begin=True)
    finally:
        await engine.dispose()
    assert seen == [False, False]  # do_execute + do_executemany: plain path


def test_the_plain_read_executor_never_touches_the_adapter() -> None:
    """A read-only plain read goes straight to the driver (its normal BEGIN)
    without reading any private adapter attribute; a write is refused."""
    from sqlalchemy import select, table, text
    from sqlalchemy.dialects import postgresql

    calls: list[str] = []

    def original(cursor: Any, statement: Any, parameters: Any, context: Any) -> str:
        calls.append(statement)
        return "ran"

    class _Cursor:  # any private attribute access would raise
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"touched cursor.{name}")

    class _Ctx:
        def __init__(self, stmt: Any) -> None:
            self.compiled = stmt.compile(dialect=postgresql.dialect())
            self.execution_options = {read_only_policy.READ_ONLY_OPTION: True}

    wrapped = read_only_policy._wrap(original)
    read = select(table("t").c)
    assert wrapped(_Cursor(), "SELECT", (), _Ctx(read)) == "ran"
    with pytest.raises(read_only_policy.ReadOnlySessionError):
        wrapped(_Cursor(), "X", (), _Ctx(text("DELETE FROM t")))
    assert calls == ["SELECT"]


def test_a_missing_pool_queue_method_is_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.pool import AsyncAdaptedQueuePool
    from sqlalchemy.util.queue import AsyncAdaptedQueue

    class Renamed(AsyncAdaptedQueue):  # type: ignore[type-arg]
        get_nowait = None  # type: ignore[assignment]

    monkeypatch.setattr(AsyncAdaptedQueuePool, "_queue_class", Renamed)
    result = driver_compat.probe(POOL_FAST_CHECKOUT)
    assert not result.ok
    assert any("get_nowait" in p for p in result.problems)


def test_a_changed_pool_queue_order_is_detected_by_the_self_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.pool import AsyncAdaptedQueuePool
    from sqlalchemy.util.queue import AsyncAdaptedQueue

    class IgnoresLifo(AsyncAdaptedQueue):  # type: ignore[type-arg]
        def __init__(self, maxsize: int = 0, use_lifo: bool = False) -> None:
            super().__init__(maxsize, use_lifo=False)

    monkeypatch.setattr(AsyncAdaptedQueuePool, "_queue_class", IgnoresLifo)
    result = driver_compat.probe(POOL_FAST_CHECKOUT)
    assert not result.ok
    assert any("order changed" in p for p in result.problems)
