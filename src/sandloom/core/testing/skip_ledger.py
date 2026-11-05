"""pytest plugin: write the tests this run skipped, and why, to a JSON ledger.

    pytest -p sandloom.core.testing.skip_ledger --sandloom-skip-ledger=skips.json

CI runs the module suites on more than one database (PostgreSQL, the
mandatory lane, and SQLite, ``second-db-test-lane``). A test may skip on a
lane that lacks what it needs (``skip_unless_db_feature``), but a test that
skips on every lane runs nowhere. Each lane writes a ledger; the
``skip-ledger`` CI job merges them (``tools/ci/skip_ledger.py``) and fails
on a test that was skipped somewhere and run nowhere, unless the allowlist
(``tools/ci/skip_ledger_allowlist.json``) names it with a reason.

The ledger (one JSON object)::

    {"version": 1, "lane": "sqlite", "ran": ["<key>", ...],
     "skipped": {"<key>": "<reason>", ...}}

A key is the test's node id, prefixed with the run's root directory relative
to ``--sandloom-skip-ledger-base`` (default: the root directory itself), so
runs from different directories of one repository share their keys. ``ran``
holds every test whose body ran (passed, failed, an expected failure);
``skipped`` every test skipped in its setup or its body, with the reason,
and every module skipped while it was collected (its key is the module's
path: the merge counts it as run when a test of that module ran elsewhere).
A test that errored in setup is in neither: the run fails anyway. Deselected
tests are not recorded. With pytest-xdist the controller writes the ledger
from the reports its workers send.

``SANDLOOM_SKIP_LEDGER`` / ``SANDLOOM_SKIP_LEDGER_LANE`` /
``SANDLOOM_SKIP_LEDGER_BASE`` stand for the options (an option wins), so a
command that runs pytest itself (``sandloom test``) writes one too::

    PYTEST_ADDOPTS="-p sandloom.core.testing.skip_ledger" \\
        SANDLOOM_SKIP_LEDGER=skips.json uv run sandloom test

Imports nothing from the framework: it only reads the reports.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

LEDGER_VERSION = 1
_ENV_PATH = "SANDLOOM_SKIP_LEDGER"
_ENV_LANE = "SANDLOOM_SKIP_LEDGER_LANE"
_ENV_BASE = "SANDLOOM_SKIP_LEDGER_BASE"
_STATE = pytest.StashKey["_Ledger"]()


class _Ledger:
    def __init__(self, path: Path, lane: str, prefix: str) -> None:
        self.path = path
        self.lane = lane
        self.prefix = prefix
        self.ran: set[str] = set()
        self.skipped: dict[str, str] = {}

    def key(self, nodeid: str) -> str:
        return f"{self.prefix}{nodeid}"

    def record(self, report: pytest.TestReport) -> None:
        key = self.key(report.nodeid)
        if report.skipped and not hasattr(report, "wasxfail"):
            if report.when in ("setup", "call"):
                self.skipped[key] = _reason(report)
                self.ran.discard(key)
            return
        if report.when == "call" and key not in self.skipped:
            self.ran.add(key)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self.record(report)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        """A module skipped while it was collected (``importorskip``,
        ``pytest.skip(..., allow_module_level=True)``): its tests never
        exist, so the module itself is the skipped key."""
        if report.skipped and report.nodeid:
            self.skipped[self.key(report.nodeid)] = _reason(report)

    def pytest_sessionfinish(self) -> None:
        self.write()

    def pytest_terminal_summary(
        self, terminalreporter: pytest.TerminalReporter
    ) -> None:
        terminalreporter.write_line(
            f"sandloom-skip-ledger: lane {self.lane}, {len(self.ran)} ran, "
            f"{len(self.skipped)} skipped -> {self.path}"
        )

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": LEDGER_VERSION,
            "lane": self.lane,
            "ran": sorted(self.ran),
            "skipped": dict(sorted(self.skipped.items())),
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
        tmp.replace(self.path)


def _reason(report: pytest.TestReport | pytest.CollectReport) -> str:
    longrepr: Any = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        reason = str(longrepr[2])
        return reason.removeprefix("Skipped: ")
    return str(longrepr or "")


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("sandloom")
    group.addoption(
        "--sandloom-skip-ledger",
        action="store",
        default=None,
        metavar="PATH",
        help="write the tests this run ran and skipped (with the reasons) to "
        f"PATH as JSON (env: {_ENV_PATH})",
    )
    group.addoption(
        "--sandloom-skip-ledger-lane",
        action="store",
        default=None,
        metavar="NAME",
        help=f"the lane this run is, recorded in the ledger (env: {_ENV_LANE})",
    )
    group.addoption(
        "--sandloom-skip-ledger-base",
        action="store",
        default=None,
        metavar="DIR",
        help="node ids are prefixed with the root directory relative to DIR "
        f"(default: the root directory; env: {_ENV_BASE})",
    )


def _option(config: pytest.Config, name: str, env: str) -> str:
    value = config.getoption(name, None)
    return str(value) if value else os.environ.get(env, "")


def _prefix(root: Path, base: str) -> str:
    if not base:
        return ""
    try:
        relative = root.resolve().relative_to(Path(base).resolve())
    except ValueError as exc:
        raise pytest.UsageError(
            f"--sandloom-skip-ledger-base={base}: the root directory {root} "
            "is not inside it"
        ) from exc
    text = relative.as_posix()
    return "" if text == "." else f"{text}/"


def pytest_configure(config: pytest.Config) -> None:
    if hasattr(config, "workerinput"):
        return  # an xdist worker: the controller records its reports
    path = _option(config, "sandloom_skip_ledger", _ENV_PATH)
    if not path:
        return
    lane = _option(config, "sandloom_skip_ledger_lane", _ENV_LANE) or "default"
    base = _option(config, "sandloom_skip_ledger_base", _ENV_BASE)
    ledger = _Ledger(
        Path(path).resolve(), lane, _prefix(Path(str(config.rootpath)), base)
    )
    config.stash[_STATE] = ledger
    config.pluginmanager.register(ledger, "sandloom-skip-ledger-run")
