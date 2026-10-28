"""``sandloom sagas`` (ADR-0008 §8, §14): the actionable list, the timeline, the
dry-run / --apply guard rails and the post-TTL refusal's exit code."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from sandloom.core.cli import sagas as cli
from sandloom.core.testing.fixtures import *  # noqa: F403 - saga_harness + DB fixtures
from sandloom.core.tests.saga_fixtures import (
    Ledger,
    enroll_input,
    enroll_saga,
    install_fakes,
)

ENROLL = enroll_saga("enroll_cli")


def _ledger(saga_harness: Any) -> Ledger:
    ledger = Ledger()
    install_fakes(saga_harness, ledger)
    return ledger


async def test_list_defaults_to_the_actionable_view(saga_harness, capsys) -> None:
    _ledger(saga_harness)
    done = await saga_harness.run(ENROLL, input=enroll_input())
    saga_harness.fail_step("notify", error="refused")
    parked = await saga_harness.run(ENROLL, input=enroll_input())

    assert await cli.run_command(["list"], saga_harness.runtime) == 0
    out = capsys.readouterr().out
    assert parked.saga_id in out and "parked (retriable_refused)" in out
    assert "notify:do" in out
    assert done.saga_id not in out

    assert await cli.run_command(["list", "--all"], saga_harness.runtime) == 0
    assert done.saga_id in capsys.readouterr().out


async def test_show_prints_a_timeline_with_the_remediation(
    saga_harness, capsys
) -> None:
    _ledger(saga_harness)
    saga_harness.fail_step("grant_access", error="refused")
    saga_harness.fail_step("charge", error="refused", direction="undo")
    run = await saga_harness.run(ENROLL, input=enroll_input())

    assert (
        await cli.run_command(
            ["show", ENROLL.saga_type, run.saga_id], saga_harness.runtime
        )
        == 0
    )
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].startswith(
        f"{ENROLL.saga_type}  {run.saga_id}  status=parked (compensation_refused)"
    )
    assert any(line.startswith("  ✓ charge") and "key saga:" in line for line in lines)
    assert any(line.startswith("  ✗ grant_access") for line in lines)
    assert any(line.startswith("  ✗ charge") and "undo" in line for line in lines)
    assert "why parked: compensation_refused" in out
    assert "undo the effect by hand at the provider" in out


async def test_mutations_are_dry_runs_without_apply(saga_harness, capsys) -> None:
    _ledger(saga_harness)
    saga_harness.fail_step("notify", error="refused")
    run = await saga_harness.run(ENROLL, input=enroll_input())

    assert (
        await cli.run_command(
            ["retry", ENROLL.saga_type, run.saga_id], saga_harness.runtime
        )
        == 0
    )
    assert "DRY-RUN" in capsys.readouterr().out
    assert (await saga_harness.instance(ENROLL, run.saga_id)).status == "parked"

    with pytest.raises(SystemExit, match="--reason"):
        await cli.run_command(
            ["retry", ENROLL.saga_type, run.saga_id, "--apply"], saga_harness.runtime
        )

    argv = ["retry", ENROLL.saga_type, run.saga_id, "--apply", "--reason", "fixed mail"]
    assert await cli.run_command(argv, saga_harness.runtime) == 0
    assert "APPLIED" in capsys.readouterr().out
    assert (await saga_harness.instance(ENROLL, run.saga_id)).status == "running"


async def test_post_ttl_retry_exits_1_with_the_resolve_hint(
    saga_harness, capsys
) -> None:
    _ledger(saga_harness)
    saga_harness.fail_step("notify", error="timeout")
    run = await saga_harness.run(
        ENROLL,
        input=enroll_input(),
        stop_when=lambda row: row.step_index == 2 and row.attempt > 0,
    )
    park = ["park", ENROLL.saga_type, run.saga_id, "--apply", "--reason", "hold it"]
    assert await cli.run_command(park, saga_harness.runtime) == 0
    saga_harness.clock.advance(timedelta(days=2))

    retry = ["retry", ENROLL.saga_type, run.saga_id, "--apply", "--reason", "try again"]
    assert await cli.run_command(retry, saga_harness.runtime) == 1
    err = capsys.readouterr().err
    assert "sandloom sagas resolve" in err and "--step notify" in err

    resolve = [
        "resolve",
        ENROLL.saga_type,
        run.saga_id,
        "--step",
        "notify",
        "--as",
        "applied",
        "--apply",
        "--reason",
        "mail went out",
    ]
    assert await cli.run_command(resolve, saga_harness.runtime) == 0
    assert "completed" in capsys.readouterr().out


def test_durations() -> None:
    assert cli.parse_duration("90s") == timedelta(seconds=90)
    assert cli.parse_duration("2h") == timedelta(hours=2)
    with pytest.raises(SystemExit):
        cli.parse_duration("soon")


async def test_doctor_warns_about_parked_and_unknown_versions(saga_harness) -> None:
    from types import SimpleNamespace

    from sandloom.core.cli import doctor
    from sandloom.core.sagas import runtime as saga_runtime

    _ledger(saga_harness)
    saga_harness.fail_step("notify", error="refused")
    await saga_harness.run(ENROLL, input=enroll_input())
    core = SimpleNamespace(saga_runtime=saga_harness.runtime)

    report = doctor.Report()
    await doctor.check_sagas(report, core, 5.0)
    statuses = {c.name: (c.status, c.message) for c in report.checks}
    assert statuses["definitions"][0] == doctor.OK
    assert statuses["database/default"][0] == doctor.WARN
    assert "1 parked" in statuses["database/default"][1]

    # an active instance of a version this process does not register
    await saga_harness.start(ENROLL, saga_id="orphan-1", input=enroll_input())
    known = saga_harness.runtime.registry
    saga_harness.runtime.registry = saga_runtime.SagaRegistry(
        [r for r in known if r.saga_type != ENROLL.saga_type]
        + [r for r in known if r.saga_type == ENROLL.saga_type]
    )
    found = await doctor.saga_ops.attention(
        saga_harness.runtime, "default", actor="test"
    )
    assert found.parked == 1 and found.unknown == []
    saga_harness.runtime.registry = saga_runtime.SagaRegistry(
        [r for r in known if r.saga_type != ENROLL.saga_type]
    )
    found = await doctor.saga_ops.attention(
        saga_harness.runtime, "default", actor="test"
    )
    assert found.unknown == [f"{ENROLL.saga_type} v1 (1)"]


async def test_doctor_skips_without_sagas() -> None:
    from types import SimpleNamespace

    from sandloom.core.cli import doctor

    report = doctor.Report()
    await doctor.check_sagas(report, SimpleNamespace(saga_runtime=None), 5.0)
    assert report.checks[0].status == doctor.SKIP
