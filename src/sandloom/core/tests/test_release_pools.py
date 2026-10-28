"""The per-test pool release covers every database provider of the app.

Pooled asyncpg connections belong to the event loop that opened them and
pytest-asyncio gives every test its own loop. The harness released only the
driver's process-wide ``default`` engines, so a provider rebuilt after a
reconfiguration (an overridden instance restored, a validation pass) kept a
connection of a closed loop and the next test using it failed with
"another operation is in progress" / "attached to a different loop" (the DX
eval's fast suite: a module's ``db()`` tests after the billing tests).
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any

from sqlalchemy import text

from sandloom.core.drivers.sqlalchemy_database import (
    create_sqlalchemy_database,
    release_default_pools,
)
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.runtime import use_active_core


def test_a_providers_own_engine_is_released_for_the_next_loop() -> None:
    provider = create_sqlalchemy_database(
        InstanceConfig(
            capability="database",
            name="release_probe",
            driver="sqlalchemy",
            options={"url": os.environ["DATABASE_URL"]},
        )
    )
    # the active application: its registry has built this provider
    core: Any = SimpleNamespace(infra=SimpleNamespace(all_built=lambda: [provider]))

    async def use() -> None:
        async with provider.session_factory() as session:
            assert (await session.execute(text("SELECT 1"))).scalar() == 1

    async def a_test_then_its_teardown() -> None:
        await use()  # leaves a pooled connection of this loop
        with use_active_core(core):
            await release_default_pools()  # the per-test teardown

    try:
        asyncio.run(a_test_then_its_teardown())
        asyncio.run(use())  # the next test's loop: no stale connection
    finally:
        asyncio.run(provider.aclose())
