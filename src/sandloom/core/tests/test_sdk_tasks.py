"""``@task`` and ``@periodic`` of ``sandloom.sdk.tasks`` (spec 8.1): the
declaration, the registry the kernel reads, the table, running a message, and
the Celery worker registration of the table."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from celery import Celery
from pydantic import BaseModel

from sandloom.core.drivers.celery_taskqueue import register_task_table
from sandloom.core.infra.errors import InvalidArgument
from sandloom.core.tasks.registry import (
    build_task_table,
    declared_periodic,
    declared_task,
    run_task,
)
from sandloom.sdk import tasks as sdk_tasks
from sandloom.sdk.tasks import periodic, task

ran: list[tuple[str, Any]] = []


class Charge(BaseModel):
    amount: int


def test_a_task_keeps_its_function_and_derives_its_name() -> None:
    @task
    async def keeps_name(payload: Charge) -> None:
        """Charge."""

    @task(queue="slow", at_least_once=True)
    def sync_one(payload: Charge) -> None:
        ran.append(("sync_one", payload.amount))

    assert keeps_name.name == f"{__name__}.keeps_name"
    assert keeps_name.__doc__ == "Charge."
    assert asyncio.iscoroutinefunction(keeps_name.__wrapped__)  # type: ignore[attr-defined]
    assert sync_one.queue == "slow"
    sync_one(Charge(amount=3))  # inline, as written
    assert ran[-1] == ("sync_one", 3)
    declared = declared_task(sync_one.name)
    assert declared is not None
    assert (declared.queue, declared.at_least_once, declared.is_async) == (
        "slow",
        True,
        False,
    )
    assert declared.payload_type is Charge


def test_the_task_api_is_the_public_surface() -> None:
    assert sorted(sdk_tasks.__all__) == [
        "PayloadlessTask",
        "PeriodicTask",
        "Task",
        "periodic",
        "task",
    ]
    for gone in ("shared_task", "cron", "TaskQueueNotInstalled"):
        assert not hasattr(sdk_tasks, gone)


def test_a_task_takes_one_pydantic_model_or_nothing() -> None:
    def two(a: Charge, b: Charge) -> None: ...

    def plain(a: int) -> None: ...

    def keyword_only(*, a: Charge) -> None: ...

    for bad in (two, plain, keyword_only):
        with pytest.raises(TypeError, match="pydantic"):
            task(bad)  # type: ignore[call-overload]


def test_two_tasks_with_one_derived_name_are_refused() -> None:
    def twice() -> None: ...

    task(twice)

    def twice() -> None:
        return None

    with pytest.raises(ValueError, match="two tasks are named"):
        task(twice)


def test_a_periodic_task_declares_cron_id_and_max_interval() -> None:
    @periodic(cron="*/5 * * * *", id="test.sdk.every-five")
    async def every_five() -> None: ...

    assert every_five.id == "test.sdk.every-five"
    assert every_five.cron == "*/5 * * * *"
    assert every_five.max_interval == timedelta(minutes=5)
    declared = declared_periodic(every_five.name)
    assert declared is not None and declared.id == "test.sdk.every-five"
    assert declared_task(every_five.name) is not None


def test_a_periodic_id_is_unique() -> None:
    @periodic(cron="0 * * * *", id="test.sdk.unique")
    def first() -> None: ...

    with pytest.raises(ValueError, match="unique"):

        @periodic(cron="0 * * * *", id="test.sdk.unique")
        def second() -> None: ...


def test_a_periodic_task_refuses_a_bad_cron_a_payload_and_an_empty_id() -> None:
    with pytest.raises(ValueError, match="expected 5 fields"):

        @periodic(cron="every five minutes", id="test.sdk.bad-cron")
        def bad_cron() -> None: ...

    with pytest.raises(TypeError, match="takes no payload"):

        @periodic(cron="0 * * * *", id="test.sdk.payload")
        def with_payload(payload: Charge) -> None: ...  # type: ignore[type-var]

    with pytest.raises(ValueError, match="needs an id"):

        @periodic(cron="0 * * * *", id="")
        def no_id() -> None: ...


def test_run_task_runs_sync_and_async_bodies_with_a_validated_payload() -> None:
    @task
    async def run_async_body(payload: Charge) -> None:
        ran.append(("async", payload.amount))

    @task
    def run_sync_body(payload: Charge) -> None:
        ran.append(("sync", payload.amount))

    @task
    def run_bare() -> None:
        ran.append(("bare", None))

    run_task(run_async_body.name, b'{"amount": 7}')
    run_task(run_sync_body.name, b'{"amount": 8}')
    run_task(run_bare.name, b"{}")
    assert ran[-3:] == [("async", 7), ("sync", 8), ("bare", None)]
    with pytest.raises(InvalidArgument):
        run_task(run_sync_body.name, b'{"amount": "many"}')
    with pytest.raises(InvalidArgument):
        run_task("test.sdk.nobody", b"{}")


async def test_an_async_body_is_refused_inside_a_running_loop() -> None:
    @task
    async def inside_loop() -> None: ...

    with pytest.raises(RuntimeError, match="running event loop"):
        run_task(inside_loop.name, b"{}")


def test_the_table_holds_the_declarations_of_the_given_modules() -> None:
    @task(queue="slow", at_least_once=True)
    async def table_task(payload: Charge) -> None: ...

    @periodic(cron="0 3 * * *", id="test.sdk.table-nightly")
    async def table_nightly() -> None: ...

    table = build_task_table([__name__])
    assert table.tasks[table_task.name].queue == "slow"
    assert table.tasks[table_task.name].at_least_once is True
    assert table.tasks[table_nightly.name].at_least_once is False
    [entry] = [p for p in table.periodic if p.id == "test.sdk.table-nightly"]
    assert (entry.cron, entry.task) == ("0 3 * * *", table_nightly.name)
    assert not build_task_table(["no_such_module.tasks"]).tasks
    assert not build_task_table([__name__ + "_other"]).tasks


async def test_enqueue_goes_through_the_kernel_taskqueue_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class Layer:
        async def enqueue(self, name: str, payload: Any, **options: Any) -> str:
            calls.append({"name": name, "payload": payload, **options})
            return "id-1"

    monkeypatch.setattr("sandloom.sdk._prim.tasks.taskqueue", lambda: Layer())

    @task
    async def enqueued(payload: Charge) -> None: ...

    @task
    async def enqueued_bare() -> None: ...

    delay = timedelta(seconds=9)
    assert (
        await enqueued.enqueue(Charge(amount=4), delay=delay, idempotency_key="k")
        == "id-1"
    )
    assert await enqueued_bare.enqueue() == "id-1"  # type: ignore[call-arg]
    assert calls[0] == {
        "name": enqueued.name,
        "payload": Charge(amount=4),
        "delay": delay,
        "idempotency_key": "k",
    }
    assert calls[1]["payload"] is None and calls[1]["delay"] is None


def test_the_celery_worker_registers_the_table() -> None:
    @task(queue="slow", at_least_once=True)
    def celery_charge(payload: Charge) -> None:
        ran.append(("celery", payload.amount))

    @periodic(cron="*/10 2 * * *", id="test.sdk.celery-beat")
    def celery_beat() -> None:
        ran.append(("beat", None))

    app = Celery("table", broker="memory://", set_as_current=False)
    try:
        register_task_table(app, build_task_table([__name__]), run_task)
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
