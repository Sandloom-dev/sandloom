"""A declared task name is only a name: it is never turned into code.

Celery's ``app.task`` builds the task signature with ``exec()`` from the
function's ``__name__``, so a name whose last dot segment is not an identifier
(``9lives``, ``do-it``) or is a keyword (``class``) used to stop the worker at
registration. The TaskTable a driver receives is a driver-door contract, so the
driver registers every name under its full declared name and runs its body.
"""

from __future__ import annotations

import pytest
from celery import Celery
from sandloom_celery.worker import register_task_table

from sandloom.core.infra.ports.taskqueue import TaskSpec, TaskTable

_NAMES = (
    "billing.tasks.plain",
    "billing.tasks.9lives",
    "billing.tasks.do-it",
    "billing.tasks.class",
    "billing.tasks.<lambda>",
    "9lives",
)


class _Boot:
    def __init__(self) -> None:
        self.ran: list[tuple[str, bytes, str]] = []

    def run(self, task: str, payload: bytes, *, task_id: str) -> None:
        self.ran.append((task, payload, task_id))


@pytest.mark.parametrize("name", _NAMES)
def test_a_task_name_that_is_not_an_identifier_registers_and_runs(name: str) -> None:
    app = Celery("task-names", broker="memory://", backend="cache+memory://")
    boot = _Boot()
    register_task_table(app, TaskTable(tasks={name: TaskSpec()}, periodic=[]), boot.run)
    app.finalize()

    assert name in app.tasks
    assert app.tasks[name].name == name
    app.tasks[name].apply(kwargs={"amount": 7}, task_id="t-1")
    assert boot.ran == [(name, b'{"amount":7}', "t-1")]


def test_every_name_of_one_table_registers_side_by_side() -> None:
    app = Celery("task-names", broker="memory://", backend="cache+memory://")
    boot = _Boot()
    tasks = {name: TaskSpec(queue="mail") for name in _NAMES}
    register_task_table(app, TaskTable(tasks=tasks, periodic=[]), boot.run)
    app.finalize()

    assert set(_NAMES) <= set(app.tasks)
    assert app.conf.task_routes == {name: {"queue": "mail"} for name in _NAMES}
