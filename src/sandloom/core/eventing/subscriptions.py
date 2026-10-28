"""Backend-neutral subscription lifecycle contract."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum


class SubscriptionState(str, Enum):
    """Observable states shared by every event-bus adapter."""

    REGISTERED = "registered"
    STARTING = "starting"
    RUNNING = "running"
    RETRYING = "retrying"
    FAILED = "failed"
    STOPPED = "stopped"


class SubscriptionUnavailableError(RuntimeError):
    """A subscription reached a terminal state before becoming ready."""


@dataclass(frozen=True, slots=True)
class SubscriptionSnapshot:
    """Point-in-time adapter state without exposing its raw client."""

    state: SubscriptionState
    attempts: int
    last_error: str | None
    last_connected_at: datetime | None


class SubscriptionHandle:
    """Lifecycle handle returned by ``EventBusProtocol.subscribe``."""

    def __init__(self, closer: Callable[[], Awaitable[None]]) -> None:
        self._closer = closer
        self._state = SubscriptionState.STARTING
        self._attempts = 0
        self._last_error: str | None = None
        self._last_connected_at: datetime | None = None
        self._ready_or_terminal = asyncio.Event()
        self._terminated = asyncio.Event()
        self._close_lock = asyncio.Lock()
        self._processing_started_at: float | None = None
        self._processing_timeout: float = 0.0

    @property
    def state(self) -> SubscriptionState:
        if self._stalled():
            return SubscriptionState.RETRYING
        return self._state

    def _stalled(self) -> bool:
        return (
            self._state is SubscriptionState.RUNNING
            and self._processing_started_at is not None
            and time.monotonic() - self._processing_started_at
            > self._processing_timeout
        )

    def mark_processing(self, timeout: float) -> None:
        self._processing_started_at = time.monotonic()
        self._processing_timeout = timeout

    def mark_progress(self) -> None:
        self._processing_started_at = None

    def snapshot(self) -> SubscriptionSnapshot:
        return SubscriptionSnapshot(
            state=self.state,
            attempts=self._attempts,
            last_error="Event handler exceeded its progress deadline"
            if self._stalled()
            else self._last_error,
            last_connected_at=self._last_connected_at,
        )

    def mark_starting(self) -> None:
        self._state = SubscriptionState.STARTING
        self._attempts += 1
        self._ready_or_terminal.clear()

    def mark_running(self) -> None:
        self._state = SubscriptionState.RUNNING
        self._last_error = None
        self._last_connected_at = datetime.now(UTC)
        self._ready_or_terminal.set()

    def mark_retrying(self, exc: BaseException) -> None:
        self._state = SubscriptionState.RETRYING
        self._attempts += 1
        self._last_error = f"{type(exc).__name__}: {exc}"
        self._ready_or_terminal.clear()

    def mark_failed(self, exc: BaseException) -> None:
        self._state = SubscriptionState.FAILED
        self._last_error = f"{type(exc).__name__}: {exc}"
        self._ready_or_terminal.set()
        self._terminated.set()

    def mark_stopped(self) -> None:
        self._state = SubscriptionState.STOPPED
        self._ready_or_terminal.set()
        self._terminated.set()

    async def wait_ready(self, timeout: float | None = None) -> None:  # noqa: ASYNC109 - explicit per-call timeout is the API
        if self.state is not SubscriptionState.RUNNING:
            waiter = self._ready_or_terminal.wait()
            if timeout is None:
                await waiter
            else:
                await asyncio.wait_for(waiter, timeout=max(0.0, timeout))
        if self.state is not SubscriptionState.RUNNING:
            raise SubscriptionUnavailableError(
                f"subscription became {self.state.value}"
            )

    async def wait_terminated(self) -> None:
        await self._terminated.wait()

    async def close(self) -> None:
        async with self._close_lock:
            if self._state is SubscriptionState.STOPPED:
                return
            try:
                await self._closer()
            finally:
                self.mark_stopped()


__all__ = [
    "SubscriptionHandle",
    "SubscriptionSnapshot",
    "SubscriptionState",
    "SubscriptionUnavailableError",
]
