"""A typed ``shared_task`` (L2): Celery's decorator, with the signature kept.

Celery ships no type information, so ``@celery.shared_task`` turns a typed
function into ``Any`` and a strict mypy refuses it ("untyped decorator")
unless the project adds an override per task module. This wrapper is the
same decorator (the task binds to whatever app the framework worker
resolves; no Celery app is built here) typed as :class:`Task`: calling it
runs the body inline, ``delay`` / ``apply_async`` enqueue it.

Celery is the optional ``sandloom[celery]`` extra, so nothing here imports it
at module import: a module whose ``tasks.py`` uses this decorator loads (and
its web process boots) on plain ``sandloom``. Where Celery is installed the
decorator hands the function to ``celery.shared_task`` exactly as before (the
worker and beat see the same task objects); where it is not, the decorated
function still runs inline and enqueueing it raises
:class:`TaskQueueNotInstalled`. ``cron`` imports Celery when it is called.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import update_wrapper
from typing import Any, NoReturn, ParamSpec, Protocol, TypeVar, cast, overload

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


#: What every refusal of this module says: the extra that provides Celery.
_INSTALL_HINT = "install the celery extra: pip install 'sandloom[celery]'"


class TaskQueueNotInstalled(RuntimeError):
    """A task was enqueued, or a beat schedule built, in a process without Celery.

    Calling the task inline still works; ``delay`` / ``apply_async`` / ``s`` /
    ``si`` and :func:`cron` need the ``sandloom[celery]`` extra.
    """


def _celery_shared_task() -> Callable[..., Any] | None:
    """``celery.shared_task``, or ``None`` when Celery is not installed.

    Only a missing ``celery`` package reads as "not installed": an ImportError
    raised inside an installed Celery propagates unchanged.
    """
    try:
        from celery import shared_task as celery_shared_task
    except ModuleNotFoundError as exc:
        if exc.name is not None and exc.name.split(".", 1)[0] == "celery":
            return None
        raise
    return cast(Callable[..., Any], celery_shared_task)


class _UnqueuedTask:
    """The :class:`Task` a decorated function becomes without Celery.

    Calling it runs the body inline, as a Celery task does; every enqueue path
    raises :class:`TaskQueueNotInstalled` naming the task, so a missing extra
    is never a silent no-op.
    """

    def __init__(self, function: Callable[..., Any], name: str | None) -> None:
        """Wrap ``function``; ``name`` defaults to Celery's ``<module>.<name>``."""
        update_wrapper(self, function)
        self._function = function
        self.name = name or f"{function.__module__}.{function.__name__}"

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Run the task body inline (no queue involved)."""
        return self._function(*args, **kwargs)

    def _refuse(self, operation: str) -> NoReturn:
        raise TaskQueueNotInstalled(
            f"task {self.name!r}: {operation} needs Celery, which is not "
            f"installed; {_INSTALL_HINT}"
        )

    def delay(self, *args: Any, **kwargs: Any) -> Any:
        """Refused: enqueueing needs Celery."""
        self._refuse("delay()")

    def apply_async(self, *args: Any, **kwargs: Any) -> Any:
        """Refused: enqueueing needs Celery."""
        self._refuse("apply_async()")

    def s(self, *args: Any, **kwargs: Any) -> Any:
        """Refused: a signature is only ever sent to a queue."""
        self._refuse("s()")

    def si(self, *args: Any, **kwargs: Any) -> Any:
        """Refused: a signature is only ever sent to a queue."""
        self._refuse("si()")


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

    Without Celery installed the function becomes a task that runs inline
    and refuses every enqueue with :class:`TaskQueueNotInstalled`.
    """
    if name is not None:
        options["name"] = name
    if acks_late is not None:
        options["acks_late"] = acks_late
    if reject_on_worker_lost is not None:
        options["reject_on_worker_lost"] = reject_on_worker_lost

    def decorate(function: Callable[..., Any]) -> Any:
        celery_shared_task = _celery_shared_task()
        if celery_shared_task is None:
            return _UnqueuedTask(function, options.get("name"))
        return cast(Any, celery_shared_task(**options)(function))

    return decorate(fn) if fn is not None else decorate


def cron(expression: str) -> Any:
    """A beat schedule from a five-field cron expression
    (``"minute hour day-of-month month day-of-week"``, e.g. ``"0 3 * * *"``)::

        def setup_periodic_tasks(sender: Any, **kwargs: Any) -> None:
            sender.add_periodic_task(cron("0 3 * * *"), purge.s(), name="m.purge")

    Raises :class:`TaskQueueNotInstalled` when Celery is not installed (a beat
    schedule only exists where Celery runs).
    """
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError(
            f"cron({expression!r}): expected 5 fields "
            "'minute hour day-of-month month day-of-week'"
        )
    minute, hour, day_of_month, month_of_year, day_of_week = fields
    try:
        from celery.schedules import crontab
    except ModuleNotFoundError as exc:
        if exc.name is None or exc.name.split(".", 1)[0] != "celery":
            raise
        raise TaskQueueNotInstalled(
            f"cron({expression!r}) needs Celery, which is not installed; "
            f"{_INSTALL_HINT}"
        ) from exc
    return crontab(
        minute=minute,
        hour=hour,
        day_of_month=day_of_month,
        month_of_year=month_of_year,
        day_of_week=day_of_week,
    )


__all__ = ["Task", "cron", "shared_task"]
