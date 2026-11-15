"""The kit's host session counts only the periodic runs of its own test.

The scheduler groups count the runs of the periodic task per slot. The broker is
shared by the tests of a run: a tick another test's scheduler left in the queue
was consumed by the next test's worker and counted as a second send for one slot
(``test_scheduler_that_takes_over_inside_a_slot_does_not_send_it_again``).
"""

from __future__ import annotations

from sandloom.driverkit.kits.taskqueue import TICK_TASK, _KitBoot, _Session


def _run(task: str, task_id: str) -> dict[str, object]:
    return {"kind": "run", "task": task, "task_id": task_id, "pid": 1}


def test_each_session_names_its_own_periodic_task() -> None:
    with _Session() as one, _Session() as two:
        assert one.tick_task != two.tick_task
        assert one.tick_task.startswith(TICK_TASK)
        table = _KitBoot(one, periodic=True).boot()
        assert one.tick_task in table.tasks
        assert two.tick_task not in table.tasks  # an unknown task runs nothing
        assert [p.task for p in table.periodic] == [one.tick_task]


def test_a_tick_left_by_another_test_is_not_counted() -> None:
    with _Session() as mine, _Session() as other:
        mine.seen.append(_run(mine.tick_task, "a"))
        mine.seen.append(_run(other.tick_task, "b"))  # a stale message
        assert [r["task_id"] for r in mine.runs(mine.tick_task)] == ["a"]
        assert [r["task_id"] for r in mine.runs()] == ["a"]
