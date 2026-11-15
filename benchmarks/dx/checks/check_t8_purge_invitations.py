"""t8_purge_invitations — a task + its periodic schedule.

Framework mode (a kernel-contributor task): the bundled ``tenancy``. App mode:
the seeded app's own ``invites`` module (``seeds/teamhub``): the same
mechanism, a nightly ``@periodic`` task in the module, idempotent, beat wired (the
app's ``sandloom test`` checks one beat per profile, SANDLOOM-T010)."""

import re

from _dx import (
    Route,
    app_mode,
    changed_under,
    module_dir,
    module_prefix,
    pick,
    reaches,
    text,
)

NAME = pick(framework="tenancy", app="invites")
MODULE = module_dir(NAME)


def _tasks() -> str:
    path = MODULE / "tasks.py"
    return path.read_text() if path.exists() else ""


def test_fn_task_and_schedule() -> None:
    src = _tasks()
    assert re.search(r"@(\w+\.)?periodic\(", src) and "cron=" in src


def test_fn_tests_changed() -> None:
    assert changed_under(module_prefix(NAME, "tests"))


def test_dev_run_async_not_asyncio_run() -> None:
    src = _tasks() + text(MODULE)
    assert ("async def" in src or "run_async(" in src) and "asyncio.run(" not in src


def test_dev_at_least_once_opt_in() -> None:
    """An idempotent task opts into at_least_once."""
    src = _tasks()
    assert re.search(r"at_least_once\s*=\s*True", src)


def test_dev_cross_tenant_work_is_explicit() -> None:
    """Purging every organization's rows is explicit cross-tenant work."""
    src = _tasks() + text(MODULE)
    assert "purge" in src.lower()
    if app_mode():
        # The seeded module's own models name Invitation already: the task's
        # code path itself leaves the bound-tenant scope explicitly.
        task = Route("", "", "tasks", MODULE / "tasks.py", _tasks())
        assert reaches(task, MODULE, ("unscoped(", "bind_tenant("))
    else:
        assert "unscoped(" in src or "Invitation" in src
