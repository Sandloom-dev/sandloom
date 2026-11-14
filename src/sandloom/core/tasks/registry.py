"""The tasks and periodic tasks a process has declared (``sandloom.sdk.tasks``).

``@task`` and ``@periodic`` call :func:`register_task` / :func:`register_periodic`
when a module's ``tasks.py`` is imported. This registry is the kernel's source
for three questions: is a task name known (the enqueue layer checks it before
reserving anything), what is the :class:`~sandloom.core.infra.ports.taskqueue.TaskTable`
of the loaded modules (the driver's host runs it), and how does a message run
(:func:`run_task`: validate the payload, call the function, an ``async def``
body on the process's persistent loop, a sync body directly).

Nothing here imports a backend: the table is data.
"""

from __future__ import annotations

import inspect
import sys
import threading
import typing
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import orjson
from pydantic import BaseModel

from sandloom.core.async_runner import run_async
from sandloom.core.infra.errors import InvalidArgument
from sandloom.core.infra.ports.taskqueue import PeriodicSpec, TaskSpec, TaskTable
from sandloom.core.tasks.cron import max_interval, parse_cron


@dataclass(frozen=True, slots=True)
class DeclaredTask:
    """One ``@task`` (or ``@periodic``) declaration."""

    name: str
    function: Callable[..., Any]
    payload_type: type[BaseModel] | None
    queue: str
    at_least_once: bool
    is_async: bool

    @property
    def module(self) -> str:
        """The module that defines the function."""
        return str(self.function.__module__)


@dataclass(frozen=True, slots=True)
class DeclaredPeriodic:
    """One ``@periodic`` declaration: the task ``name`` runs at ``cron``."""

    id: str
    name: str
    cron: str
    max_interval: timedelta
    queue: str


_lock = threading.Lock()
_tasks: dict[str, DeclaredTask] = {}
_periodic: dict[str, DeclaredPeriodic] = {}


def task_name(function: Callable[..., Any]) -> str:
    """The derived task name: ``<module>.<function name>``."""
    return f"{function.__module__}.{function.__name__}"


def _same_definition(a: Callable[..., Any], b: Callable[..., Any]) -> bool:
    """Is ``b`` the same function as ``a``, executed again?

    A module imported twice (a reload) defines the function again at the same
    file and line; a module dropped from ``sys.modules`` and imported afresh
    (a test app rebuilt in a new directory under one package name) replaces its
    own earlier entry. Anything else is a different function that derived the
    same name (another file while the first module is live, or another line of
    one file): the second must not silently replace the first, so it is refused.
    """
    code_a, code_b = getattr(a, "__code__", None), getattr(b, "__code__", None)
    if code_a is None or code_b is None:
        return False
    if (
        code_a.co_filename == code_b.co_filename
        and code_a.co_firstlineno == code_b.co_firstlineno
    ):
        return True
    return _module_gone(a)


def _module_gone(function: Callable[..., Any]) -> bool:
    """Was the module that defined ``function`` dropped from ``sys.modules``?

    True when the live module of that name no longer owns the function's
    globals (it was removed, or replaced by a fresh import).
    """
    live = sys.modules.get(function.__module__)
    return live is None or getattr(live, "__dict__", None) is not getattr(
        function, "__globals__", None
    )


def payload_type_of(function: Callable[..., Any]) -> type[BaseModel] | None:
    """The pydantic model a task function takes, ``None`` for no parameter.

    Raises ``TypeError`` for any other shape: a task takes one pydantic model
    of wire types, or nothing.
    """
    name = task_name(function)
    parameters = list(inspect.signature(function).parameters.values())
    if not parameters:
        return None
    if len(parameters) > 1 or parameters[0].kind not in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    ):
        raise TypeError(
            f"task {name!r} takes one pydantic payload model or nothing, "
            f"got {inspect.signature(function)}"
        )
    try:
        hints = typing.get_type_hints(function)
    except Exception as exc:
        raise TypeError(
            f"task {name!r}: its payload annotation cannot be resolved "
            f"({type(exc).__name__}: {exc})"
        ) from exc
    annotation = hints.get(parameters[0].name)
    if not (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
        raise TypeError(
            f"task {name!r}: the payload parameter {parameters[0].name!r} must be "
            f"annotated with a pydantic model, got {annotation!r}"
        )
    return annotation


def register_task(declared: DeclaredTask) -> None:
    """Record ``declared``; two functions with one derived name are refused."""
    with _lock:
        known = _tasks.get(declared.name)
        if known is not None and not _same_definition(
            known.function, declared.function
        ):
            raise ValueError(
                f"two tasks are named {declared.name!r} (a function defined twice "
                "in one module): rename one"
            )
        _tasks[declared.name] = declared


def register_periodic(declared: DeclaredPeriodic) -> None:
    """Record ``declared``; a periodic id used twice is refused."""
    with _lock:
        known = _periodic.get(declared.id)
        if known is not None and known.name != declared.name:
            raise ValueError(
                f"the periodic id {declared.id!r} is declared by {known.name!r} "
                f"and by {declared.name!r}: an id is unique in the deployment"
            )
        _periodic[declared.id] = declared


def declared_task(name: str) -> DeclaredTask | None:
    """The declaration of the task ``name``, ``None`` when this process has none."""
    return _tasks.get(name)


def declared_periodic(task: str) -> DeclaredPeriodic | None:
    """The periodic declaration of the task ``task``."""
    for declared in _periodic.values():
        if declared.name == task:
            return declared
    return None


def _in_modules(module: str, task_modules: tuple[str, ...]) -> bool:
    return any(module == m or module.startswith(m + ".") for m in task_modules)


def build_task_table(task_modules: Iterable[str]) -> TaskTable:
    """The table of the declarations made in ``task_modules`` (their import
    names, ``"sandloom_billing.tasks"``) and the modules below them."""
    wanted = tuple(task_modules)
    with _lock:
        tasks = {
            name: TaskSpec(queue=d.queue, at_least_once=d.at_least_once)
            for name, d in sorted(_tasks.items())
            if _in_modules(d.module, wanted)
        }
        periodic = tuple(
            PeriodicSpec(id=p.id, cron=p.cron, task=p.name, queue=p.queue)
            for p in sorted(_periodic.values(), key=lambda p: p.id)
            if p.name in tasks
        )
    return TaskTable(tasks=tasks, periodic=periodic)


def encode_payload(
    declared: DeclaredTask, payload: BaseModel | Mapping[str, Any] | None
) -> bytes:
    """``payload`` validated against the task's model and encoded as JSON.

    A task without a payload model takes ``None`` or an empty mapping and
    encodes ``b"{}"``. Raises :class:`InvalidArgument` when it does not
    validate or cannot be encoded; nothing was reserved or sent yet.
    """
    try:
        if declared.payload_type is None:
            if payload is not None and not (
                isinstance(payload, Mapping) and not payload
            ):
                raise ValueError("the task takes no payload")
            return b"{}"
        model = (
            payload
            if isinstance(payload, declared.payload_type)
            else declared.payload_type.model_validate(
                payload.model_dump(by_alias=True)
                if isinstance(payload, BaseModel)
                else payload
            )
        )
        # By alias: the worker validates this JSON against the same model, whose
        # aliased fields are read by their alias.
        return orjson.dumps(model.model_dump(mode="json", by_alias=True))
    except (ValueError, TypeError) as exc:
        # ``from None``: a pydantic error holds the input values, and a task
        # payload is data (never in a log or a chained traceback).
        raise InvalidArgument(
            f"payload of task {declared.name!r} is refused ({type(exc).__name__})",
            capability="taskqueue",
        ) from None


def run_task(name: str, payload: bytes) -> None:
    """Run the task ``name`` once with the JSON ``payload``, outside any running loop.

    An ``async def`` body runs on the process's persistent loop (``run_async``),
    a sync body directly. Raises :class:`InvalidArgument` for an unknown task
    or a payload that does not validate; the body's own exception propagates.
    """
    declared = _tasks.get(name)
    if declared is None:
        raise InvalidArgument(
            f"task {name!r} is not declared in this process", capability="taskqueue"
        )
    arguments: tuple[Any, ...] = ()
    if declared.payload_type is not None:
        try:
            arguments = (declared.payload_type.model_validate_json(payload),)
        except (ValueError, TypeError) as exc:
            # ``from None``: a pydantic error holds the input values, and a task
            # payload is data (never in a log or a chained traceback).
            raise InvalidArgument(
                f"payload of task {name!r} is refused ({type(exc).__name__})",
                capability="taskqueue",
            ) from None
    if declared.is_async:
        run_async(declared.function(*arguments))
    else:
        declared.function(*arguments)


def declare(
    function: Callable[..., Any],
    *,
    queue: str,
    at_least_once: bool,
    cron: str | None = None,
    periodic_id: str | None = None,
) -> tuple[DeclaredTask, DeclaredPeriodic | None]:
    """Validate and register ``function`` as a task, and as a periodic one when
    ``cron`` is given. Raises ``TypeError`` / ``ValueError`` at the decorator."""
    if not isinstance(queue, str) or not queue:
        raise ValueError(f"task {task_name(function)!r}: queue must be a name")
    payload_type = payload_type_of(function)
    periodic: DeclaredPeriodic | None = None
    if cron is not None:
        if payload_type is not None:
            raise TypeError(
                f"periodic task {task_name(function)!r} takes no payload "
                "(a periodic run has none)"
            )
        if not isinstance(periodic_id, str) or not periodic_id:
            raise ValueError(f"periodic task {task_name(function)!r} needs an id")
        parse_cron(cron)
        periodic = DeclaredPeriodic(
            id=periodic_id,
            name=task_name(function),
            cron=" ".join(cron.split()),
            max_interval=max_interval(cron),
            queue=queue,
        )
    declared = DeclaredTask(
        name=task_name(function),
        function=function,
        payload_type=payload_type,
        queue=queue,
        at_least_once=at_least_once,
        is_async=inspect.iscoroutinefunction(function),
    )
    register_task(declared)
    if periodic is not None:
        register_periodic(periodic)
    return declared, periodic


__all__ = [
    "DeclaredPeriodic",
    "DeclaredTask",
    "build_task_table",
    "declare",
    "declared_periodic",
    "declared_task",
    "encode_payload",
    "payload_type_of",
    "register_periodic",
    "register_task",
    "run_task",
    "task_name",
]
