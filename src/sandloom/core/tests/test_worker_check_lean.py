"""``sandloom worker check`` on an install that cannot answer.

A lean install has no taskqueue driver (SANDLOOM-B037), or INFRA names no
taskqueue instance: the healthcheck exits 1 with the code and the fix on
stderr, never a traceback. Any other failure of the check still raises.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sandloom.core.cli import worker as worker_cli
from sandloom.core.infra.registry import (
    DriverUnavailableError,
    InstanceNotConfiguredError,
)


def _infra_failing_with(error: Exception) -> SimpleNamespace:
    def provider(capability: str, name: str) -> None:
        raise error

    return SimpleNamespace(provider=provider)


@pytest.mark.parametrize(
    ("error", "shown"),
    [
        (
            DriverUnavailableError(
                "taskqueue/default: driver 'celery' is not installed",
                fix="uv add sandloom-celery",
            ),
            ["SANDLOOM-B037", "uv add sandloom-celery"],
        ),
        (
            InstanceNotConfiguredError(
                "no taskqueue instance 'default'", fix="set INFRA.taskqueue.default"
            ),
            ["no taskqueue instance 'default'", "set INFRA.taskqueue.default"],
        ),
    ],
    ids=["driver-unavailable", "instance-not-configured"],
)
def test_the_check_prints_the_refusal_and_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    shown: list[str],
) -> None:
    monkeypatch.setattr(
        worker_cli._tool_infra, "build_tool_infra", lambda: _infra_failing_with(error)
    )
    assert worker_cli.worker_main(["check"]) == 1
    err = capsys.readouterr().err
    assert all(part in err for part in shown)
    assert "Traceback" not in err


def test_another_failure_of_the_check_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker_cli._tool_infra,
        "build_tool_infra",
        lambda: _infra_failing_with(RuntimeError("a bug, not a refusal")),
    )
    with pytest.raises(RuntimeError, match="a bug"):
        worker_cli.worker_main(["check"])
