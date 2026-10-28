"""Fixtures for the performance-budget tests (deterministic counts, no timings).

``counted_client`` is the harness ``real_uow_client`` (every request runs the
REAL request UoW: commit/rollback, contract pools, post-commit dispatch) with
the app's ``default`` database pointed THROUGH :class:`PgCountingProxy`, so a
test can count exactly how many round trips a request costs.

``sql_counter`` counts SQL statements on every engine
(``before_cursor_execute``); Redis commands are counted by wrapping the
client's ``execute_command`` (``_counters.RedisCommandCounter``).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url

from sandloom.core.testing.fixtures import *  # noqa: F403

from ._counters import SqlStatementCounter
from .pgcount import PgCountingProxy

WEBHOOK_SECRET = "whsec_perf_budget"


@pytest.fixture(scope="session", autouse=True)
def _cheap_password_hashing() -> Iterator[None]:
    """Same argon2id scheme, minimal work factor (budgets count I/O only)."""
    from argon2 import PasswordHasher

    from sandloom_identity.services.auth_service import AuthService

    original = AuthService.password_hasher
    AuthService.password_hasher = PasswordHasher(
        time_cost=1, memory_cost=8, parallelism=1
    )
    yield
    AuthService.password_hasher = original


@pytest.fixture
def sql_counter() -> Iterator[SqlStatementCounter]:
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    counter = SqlStatementCounter()
    # event.remove leaves Engine._has_events set; restore the class flag so
    # later tests that pin the event-free fast path keep passing.
    had_events = Engine._has_events
    event.listen(Engine, "before_cursor_execute", counter)
    try:
        yield counter
    finally:
        event.remove(Engine, "before_cursor_execute", counter)
        Engine._has_events = had_events


@pytest_asyncio.fixture
async def pg_proxy() -> AsyncIterator[PgCountingProxy]:
    url = make_url(os.environ["DATABASE_URL"])
    proxy = PgCountingProxy(url.host or "127.0.0.1", int(url.port or 5432))
    await proxy.start()
    try:
        yield proxy
    finally:
        await proxy.stop()


def proxied_url(proxy: PgCountingProxy) -> str:
    url = make_url(os.environ["DATABASE_URL"]).set(host="127.0.0.1", port=proxy.port)
    return url.render_as_string(hide_password=False)


@pytest.fixture
def fake_psp() -> Iterator[Any]:
    from sandloom_billing.psp import FakeStripeClient, install_psp_client

    psp = FakeStripeClient(webhook_secret=WEBHOOK_SECRET)
    previous = install_psp_client(psp)
    yield psp
    install_psp_client(previous)


@pytest_asyncio.fixture
async def counted_client(
    sandloom_app: Any,
    sandloom_core: Any,
    db_engine: Any,
    pg_proxy: PgCountingProxy,
    fake_psp: Any,
) -> AsyncIterator[Any]:
    """HTTP client on the real request UoW, its database behind the proxy."""
    import httpx

    from sandloom.core._internal.db_provider import get_db
    from sandloom.core.testing.infra import override_instance

    assert get_db not in sandloom_app.dependency_overrides
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(
            override_instance(
                sandloom_core,
                "database",
                driver="sqlalchemy",
                url=proxied_url(pg_proxy),
            )
        )
        await stack.enter_async_context(
            override_instance(
                sandloom_core, "cache", driver="redis", url=os.environ["CACHE_URL"]
            )
        )
        http = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(
                    app=sandloom_app, raise_app_exceptions=False
                ),
                base_url="http://test",
            )
        )
        yield http
