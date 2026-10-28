"""Bound connection-establishment storms after a PostgreSQL outage.

This dialect do_connect hook never retries a query or a transaction. Each
caller attempts ONE connection; consecutive transport failures schedule the
next pool-wide probe with capped exponential backoff and equal jitter. Only
one probe per pool runs during recovery. Healthy connection creation is direct.
"""

from __future__ import annotations

import asyncio
import random
import socket
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy import event
from sqlalchemy.util.concurrency import await_

from sandloom.core.exceptions import DatabaseUnavailable
from sandloom.core.sql_alchemy.dead_connection import is_dead_connection_error


class ReconnectBackoff:
    def __init__(
        self,
        *,
        base: float,
        cap: float,
        wait_timeout: float,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._base, self._cap = base, cap
        self._wait_timeout = wait_timeout
        self._clock, self._jitter = clock, jitter
        self._failures = 0
        self._next_probe = 0.0
        self._gate = asyncio.Lock()

    def _failed(self) -> None:
        self._failures = min(self._failures + 1, 32)
        ceiling = min(self._cap, self._base * 2 ** (self._failures - 1))
        self._next_probe = self._clock() + self._jitter(ceiling / 2, ceiling)

    def invalidated(self, connection: Any, record: Any, error: Any) -> None:
        # This also covers raw planner cancellation/disconnection cleanup and
        # idle-ping invalidation, where no dialect handle_error event runs.
        # A cancelled/unknown connection is conservatively a recovery boundary.
        self._failed()

    async def _admit(self) -> None:
        async with asyncio.timeout(self._wait_timeout):
            await self._gate.acquire()
            try:
                delay = self._next_probe - self._clock()
                if delay > 0:
                    await asyncio.sleep(delay)
            except BaseException:
                self._gate.release()
                raise

    def connect(self, dialect: Any, _record: Any, args: Any, params: Any) -> Any:
        guarded = self._failures > 0
        if guarded:
            try:
                await_(self._admit())
            except TimeoutError as error:
                raise DatabaseUnavailable(error) from error
        try:
            try:
                connection = dialect.connect(*args, **params)
            except Exception as error:
                if not (
                    is_dead_connection_error(error)
                    or isinstance(error, (TimeoutError, socket.gaierror))
                    or getattr(error, "sqlstate", None) == "53300"
                ):
                    raise
                self._failed()
                raise DatabaseUnavailable(error) from error
            self._failures, self._next_probe = 0, 0.0
            return connection
        finally:
            if guarded:
                self._gate.release()


def install_reconnect_backoff(
    engine: Any, *, base: float, cap: float, wait_timeout: float
) -> None:
    """Install once on a newly built asyncpg engine (public SQLAlchemy event)."""
    if engine.dialect.driver == "asyncpg":
        gate = ReconnectBackoff(base=base, cap=cap, wait_timeout=wait_timeout)
        event.listen(engine.sync_engine, "do_connect", gate.connect)
        event.listen(engine.sync_engine, "invalidate", gate.invalidated)
