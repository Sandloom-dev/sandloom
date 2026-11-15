"""Sandloom's own task API (L2): ``@task`` and ``@periodic``.

A task is a module-level function in ``<module>/tasks.py``, ``async def`` (new
code) or plain ``def``, taking one pydantic model of wire types or nothing. Its
name is ``f"{fn.__module__}.{fn.__name__}"``; two tasks with one name are
refused when the module loads. Calling a task runs the function inline, in the
caller's process; ``await task.enqueue(payload)`` sends it to the taskqueue
instead, where the worker runs it.

``@periodic(cron="*/5 * * * *", id="billing.reconcile-intents")`` declares a
task without a payload that the scheduler sends at the slots of a five-field
cron expression (UTC); ``id`` is unique in the deployment. Nothing here imports
a broker: the declarations are data the kernel's worker reads
(:mod:`sandloom.core.tasks.registry`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from functools import update_wrapper
from typing import Any, Protocol, TypeVar, overload

from pydantic import BaseModel

from sandloom.core.di import declare_task, taskqueue

P = TypeVar("P", bound=BaseModel, contravariant=True)
R = TypeVar("R", None, Awaitable[None], covariant=True)


class Task(Protocol[P, R]):
    """A declared task: call it inline, or enqueue it."""

    name: str
    queue: str

    async def enqueue(
        self,
        payload: P,
        *,
        delay: timedelta | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Send the task to the taskqueue; returns the task id.

        ``delay`` needs the taskqueue feature ``delay``. With
        ``idempotency_key`` a repeat returns the first task id (a different
        payload under one key conflicts).
        """
        ...

    def __call__(self, payload: P) -> R:
        """Run the function inline (an ``async def`` returns its coroutine)."""
        ...


class PayloadlessTask(Protocol[R]):
    """A declared task that takes no payload."""

    name: str
    queue: str

    async def enqueue(
        self,
        *,
        delay: timedelta | None = None,
        idempotency_key: str | None = None,
    ) -> str: ...

    def __call__(self) -> R: ...


class PeriodicTask(Protocol[R]):
    """A task the scheduler sends at the slots of its cron expression."""

    name: str
    id: str
    cron: str
    queue: str
    #: The longest gap between two consecutive slots of ``cron``.
    max_interval: timedelta

    def __call__(self) -> R: ...


class _TaskDecorator(Protocol):
    """What ``@task(queue=..., at_least_once=...)`` returns: it types the
    function it decorates, with a payload or without."""

    @overload
    def __call__(self, fn: Callable[[P], R], /) -> Task[P, R]: ...

    @overload
    def __call__(self, fn: Callable[[], R], /) -> PayloadlessTask[R]: ...


class _Task:
    """What ``@task`` returns: the function, plus ``name`` and ``enqueue``."""

    def __init__(
        self, function: Callable[..., Any], *, queue: str, at_least_once: bool
    ) -> None:
        declared, _ = declare_task(function, queue=queue, at_least_once=at_least_once)
        update_wrapper(self, function)
        self._function = function
        self.name = declared.name
        self.queue = queue

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._function(*args, **kwargs)

    async def enqueue(
        self,
        payload: BaseModel | None = None,
        *,
        delay: timedelta | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        # The model itself: the kernel validates it and encodes it by alias.
        return await taskqueue().enqueue(
            self.name, payload, delay=delay, idempotency_key=idempotency_key
        )


class _PeriodicTask:
    """What ``@periodic`` returns: the function, plus its declaration."""

    def __init__(
        self,
        function: Callable[..., Any],
        *,
        cron: str,
        id: str,
        queue: str,
        at_least_once: bool,
    ) -> None:
        declared, periodic = declare_task(
            function,
            queue=queue,
            at_least_once=at_least_once,
            cron=cron,
            periodic_id=id,
        )
        if periodic is None:  # declare() returns it for a cron
            raise RuntimeError(f"periodic task {declared.name!r} was not declared")
        update_wrapper(self, function)
        self._function = function
        self.name = declared.name
        self.id = periodic.id
        self.cron = periodic.cron
        self.queue = queue
        self.max_interval = periodic.max_interval

    def __call__(self) -> Any:
        return self._function()


@overload
def task(fn: Callable[[P], R], /) -> Task[P, R]: ...


@overload
def task(fn: Callable[[], R], /) -> PayloadlessTask[R]: ...


@overload
def task(*, queue: str = "default", at_least_once: bool = False) -> _TaskDecorator: ...


def task(
    fn: Callable[..., Any] | None = None,
    /,
    *,
    queue: str = "default",
    at_least_once: bool = False,
) -> Any:
    """``@task`` / ``@task(queue=..., at_least_once=True)``.

    ``at_least_once=True`` redelivers the task when its worker dies mid-run:
    only for a task whose second run is harmless (it needs the taskqueue
    feature ``redelivery``, derived at load). Its effects still need a
    DB-atomic dedupe (``durable_inbox`` or a UNIQUE operation key).
    """

    def decorate(function: Callable[..., Any]) -> Any:
        return _Task(function, queue=queue, at_least_once=at_least_once)

    return decorate(fn) if fn is not None else decorate


def periodic(
    *,
    cron: str,
    id: str,
    queue: str = "default",
    at_least_once: bool = False,
) -> Callable[[Callable[[], R]], PeriodicTask[R]]:
    """``@periodic(cron="0 3 * * *", id="identity.cleanup")``: a task without a
    payload, sent at the slots of ``cron`` (five fields, UTC) while a
    scheduler runs. ``id`` is unique in the deployment. A run may repeat, so
    the body tolerates a second run. The module needs the taskqueue feature
    ``periodic`` (derived at load).
    """

    def decorate(function: Callable[[], R]) -> PeriodicTask[R]:
        return _PeriodicTask(
            function, cron=cron, id=id, queue=queue, at_least_once=at_least_once
        )

    return decorate


__all__ = ["PayloadlessTask", "PeriodicTask", "Task", "periodic", "task"]
