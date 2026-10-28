"""A typed ``shared_task`` (L2): Celery's decorator, with the signature kept.

Celery ships no type information, so ``@celery.shared_task`` turns a typed
function into ``Any`` and a strict mypy refuses it ("untyped decorator")
unless the project adds an override per task module. This wrapper is the
same decorator (the task binds to whatever app the framework worker
resolves; no Celery app is built here) typed as :class:`Task`: calling it
runs the body inline, ``delay`` / ``apply_async`` enqueue it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ParamSpec, Protocol, TypeVar, cast, overload

from celery import shared_task as _celery_shared_task
from celery.schedules import crontab as _crontab

P = ParamSpec("P")
R = TypeVar("R", covariant=True)


class Task(Protocol[P, R]):
    """A registered task: call it inline, or enqueue it."""

    name: str

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R: ...

    def delay(self, *args: P.args, **kwargs: P.kwargs) -> Any: ...

    def s(self, *args: Any, **kwargs: Any) -> Any:
        """A signature (``sender.add_periodic_task(schedule, task.s())``)."""
        ...

    def si(self, *args: Any, **kwargs: Any) -> Any: ...

    def apply_async(
        self,
        args: tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
        **options: Any,
    ) -> Any: ...


@overload
def shared_task(fn: Callable[P, R], /) -> Task[P, R]: ...


@overload
def shared_task(
    *,
    name: str | None = None,
    acks_late: bool | None = None,
    reject_on_worker_lost: bool | None = None,
    **options: Any,
) -> Callable[[Callable[P, R]], Task[P, R]]: ...


def shared_task(
    fn: Callable[..., Any] | None = None,
    /,
    *,
    name: str | None = None,
    acks_late: bool | None = None,
    reject_on_worker_lost: bool | None = None,
    **options: Any,
) -> Any:
    """``@shared_task`` / ``@shared_task(name=..., acks_late=True, ...)``.

    An idempotent, at-least-once task (redelivered when a worker dies) is
    ``@shared_task(acks_late=True, reject_on_worker_lost=True)``; its body
    runs async work through ``run_async(...)`` with ``open_uow()`` blocks.
    """
    if name is not None:
        options["name"] = name
    if acks_late is not None:
        options["acks_late"] = acks_late
    if reject_on_worker_lost is not None:
        options["reject_on_worker_lost"] = reject_on_worker_lost

    def decorate(function: Callable[..., Any]) -> Any:
        return cast(Any, _celery_shared_task(**options)(function))

    return decorate(fn) if fn is not None else decorate


def cron(expression: str) -> Any:
    """A beat schedule from a five-field cron expression
    (``"minute hour day-of-month month day-of-week"``, e.g. ``"0 3 * * *"``)::

        def setup_periodic_tasks(sender: Any, **kwargs: Any) -> None:
            sender.add_periodic_task(cron("0 3 * * *"), purge.s(), name="m.purge")
    """
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError(
            f"cron({expression!r}): expected 5 fields "
            "'minute hour day-of-month month day-of-week'"
        )
    minute, hour, day_of_month, month_of_year, day_of_week = fields
    return _crontab(
        minute=minute,
        hour=hour,
        day_of_month=day_of_month,
        month_of_year=month_of_year,
        day_of_week=day_of_week,
    )


__all__ = ["Task", "cron", "shared_task"]
