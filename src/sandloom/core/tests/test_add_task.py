"""``sandloom add task``: the cron is parsed with the scheduler's grammar and a
plain task's docstring states the delivery it really has."""

from __future__ import annotations

from pathlib import Path

import pytest

from sandloom.core.cli import add
from sandloom.core.cli.project import generate_project


def _where(tmp_path: Path) -> add.Layout:
    generate_project("probe", tmp_path, sample_module="notes")
    return add.layout(tmp_path / "probe")


@pytest.mark.parametrize(
    "cron", ["99 * * * *", "* * * * 9", "a b c d e", "*/0 * * * *", "0 3 * *"]
)
def test_periodic_refuses_a_cron_the_scheduler_would_refuse(
    tmp_path: Path, cron: str
) -> None:
    where = _where(tmp_path)
    tasks = tmp_path / "probe" / "probe" / "modules" / "notes" / "tasks.py"
    with pytest.raises(add.GeneratorError, match="--periodic"):
        add.add_task("notes", "purge", where, periodic=cron)
    assert not tasks.exists()


def test_a_plain_task_states_at_most_once_and_omits_at_least_once(
    tmp_path: Path,
) -> None:
    where = _where(tmp_path)
    add.add_task("notes", "reindex", where)
    source = (
        tmp_path / "probe" / "probe" / "modules" / "notes" / "tasks.py"
    ).read_text()
    assert "at most once unless" in source and "at_least_once=True" in source
    assert "A run can repeat" not in source
    assert "@task\nasync def reindex" in source
    assert "at_least_once=True)" not in source and "(at_least_once" not in source


def test_a_periodic_task_keeps_the_repeat_warning(tmp_path: Path) -> None:
    where = _where(tmp_path)
    add.add_task("notes", "purge", where, periodic="0 3 * * *")
    source = (
        tmp_path / "probe" / "probe" / "modules" / "notes" / "tasks.py"
    ).read_text()
    assert "A run can repeat or overlap" in source
