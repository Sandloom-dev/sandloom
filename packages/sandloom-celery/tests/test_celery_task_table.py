"""The Celery worker registers the table the kernel builds from ``@task`` and
``@periodic`` declarations (spec 8.1). The declarations and the table itself are
``sandloom.core.tests.test_sdk_tasks``."""

from __future__ import annotations

from typing import Any

from celery import Celery
from pydantic import BaseModel
from sandloom_celery.worker import register_task_table

from sandloom.core.tasks.registry import build_task_table, run_task
from sandloom.sdk.tasks import periodic, task

ran: list[tuple[str, Any]] = []


class Charge(BaseModel):
    amount: int


def test_the_celery_worker_registers_the_table() -> None:
    @task(queue="slow", at_least_once=True)
    def celery_charge(payload: Charge) -> None:
        ran.append(("celery", payload.amount))

    @periodic(cron="*/10 2 * * *", id="test.sdk.celery-beat")
    def celery_beat() -> None:
        ran.append(("beat", None))

    app = Celery("table", broker="memory://", set_as_current=False)
    try:
        register_task_table(
            app,
            build_task_table([__name__]),
            lambda name, payload, *, task_id: run_task(name, payload),
        )
        app.finalize()
        registered = app.tasks[celery_charge.name]
        assert registered.acks_late and registered.reject_on_worker_lost
        assert app.tasks[celery_beat.name].acks_late is False
        assert app.conf.task_routes[celery_charge.name] == {"queue": "slow"}
        registered.run(amount=11)  # the message's kwargs are the payload
        assert ran[-1] == ("celery", 11)
        entry = app.conf.beat_schedule["test.sdk.celery-beat"]
        assert entry["task"] == celery_beat.name
        assert entry["schedule"].minute == set(range(0, 60, 10))
        assert entry["schedule"].hour == {2}
    finally:
        app.close()
