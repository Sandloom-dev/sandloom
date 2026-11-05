"""``tools/ci/skip_ledger.py``: a test skipped on every CI lane fails CI.

PostgreSQL is the mandatory lane, SQLite the second (``second-db-test-lane``);
each writes a ledger (``sandloom.core.testing.skip_ledger``) and the
``skip-ledger`` job merges them.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "skip_ledger_tool", ROOT / "tools/ci/skip_ledger.py"
)
assert _spec and _spec.loader
tool = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = tool  # its dataclass resolves its module
_spec.loader.exec_module(tool)

ALLOWLIST = ROOT / "tools/ci/skip_ledger_allowlist.json"


def _ledger(
    path: Path, lane: str, ran: list[str], skipped: dict[str, str] | None = None
) -> Path:
    path.write_text(
        json.dumps({"version": 1, "lane": lane, "ran": ran, "skipped": skipped or {}})
    )
    return path


def _allow(path: Path, *entries: dict[str, str]) -> Path:
    path.write_text(json.dumps({"entries": list(entries)}))
    return path


def test_a_test_skipped_on_one_lane_and_run_on_another_passes(tmp_path, capsys):
    pg = _ledger(tmp_path / "pg.json", "postgresql", ["t.py::race", "t.py::a"])
    lite = _ledger(
        tmp_path / "lite.json", "sqlite", ["t.py::a"], {"t.py::race": "skip_locked"}
    )
    assert tool.main([str(pg), str(lite), "--require-lanes", "postgresql,sqlite"]) == 0
    assert "skip ledger: OK" in capsys.readouterr().out


def test_a_test_skipped_on_every_lane_fails_with_each_lanes_reason(tmp_path, capsys):
    pg = _ledger(tmp_path / "pg.json", "postgresql", ["t.py::a"], {"t.py::b": "no x"})
    lite = _ledger(tmp_path / "lite.json", "sqlite", [], {"t.py::b": "no y"})
    assert tool.main([str(pg), str(lite)]) == 1
    out = capsys.readouterr().out
    assert "FAIL skipped on every lane: t.py::b [postgresql: no x; sqlite: no y]" in out


def test_shards_of_one_lane_are_judged_together(tmp_path):
    # Part 1 skipped it, part 2 never collected it, SQLite ran it: fine.
    part1 = _ledger(tmp_path / "p1.json", "postgresql", [], {"t.py::b": "x"})
    part2 = _ledger(tmp_path / "p2.json", "postgresql", ["t.py::a"])
    lite = _ledger(tmp_path / "lite.json", "sqlite", ["t.py::b"])
    assert tool.main([str(part1), str(part2), str(lite)]) == 0
    # A test only one shard collected, and skipped there, runs nowhere.
    assert tool.main([str(part1), str(part2)]) == 1


def test_the_allowlist_excuses_by_name_or_pattern_with_a_reason(tmp_path, capsys):
    pg = _ledger(
        tmp_path / "pg.json",
        "postgresql",
        [],
        {"t.py::macos": "macOS only", "e2e/t.py::x[1]": "needs docker"},
    )
    allow = _allow(
        tmp_path / "allow.json",
        {"test": "t.py::macos", "reason": "CI runs Linux"},
        {"test": "e2e/t.py::x[*]", "reason": "no Docker in the job"},
    )
    assert tool.main([str(pg), "--allowlist", str(allow)]) == 0
    out = capsys.readouterr().out
    assert "allowed: t.py::macos (CI runs Linux)" in out


def test_an_allowlist_entry_without_a_reason_or_whose_test_ran_fails(tmp_path, capsys):
    pg = _ledger(tmp_path / "pg.json", "postgresql", ["t.py::a", "t.py::now_runs"])
    allow = _allow(
        tmp_path / "allow.json",
        {"test": "t.py::now_runs", "reason": "it was skipped once"},
        {"test": "t.py::a"},
    )
    assert tool.main([str(pg), "--allowlist", str(allow)]) == 1
    out = capsys.readouterr().out
    assert "names a test that ran (remove it): t.py::now_runs" in out
    assert "allowlist entry without a test or a reason" in out


def test_an_allowlist_entry_no_lane_collected_is_not_judged(tmp_path):
    # The PR profile deselects the slow suites an entry names.
    pg = _ledger(tmp_path / "pg.json", "postgresql", ["t.py::a"])
    allow = _allow(
        tmp_path / "allow.json", {"test": "slow/t.py::*", "reason": "opt-in"}
    )
    assert tool.main([str(pg), "--allowlist", str(allow)]) == 0


def test_a_module_skipped_at_collection_runs_where_one_of_its_tests_ran(
    tmp_path, capsys
):
    pg = _ledger(tmp_path / "pg.json", "postgresql", ["m/t.py::a"])
    lite = _ledger(
        tmp_path / "lite.json",
        "sqlite",
        [],
        {"m/t.py": "no library", "m/gone.py": "no library"},
    )
    assert tool.main([str(pg), str(lite)]) == 1
    out = capsys.readouterr().out
    assert "skipped on every lane: m/gone.py" in out
    assert "m/t.py [" not in out


def test_strict_allowlist_fails_an_entry_no_lane_collected(tmp_path, capsys):
    pg = _ledger(tmp_path / "pg.json", "postgresql", ["t.py::a"])
    allow = _allow(tmp_path / "allow.json", {"test": "gone.py::*", "reason": "x"})
    assert tool.main([str(pg), "--allowlist", str(allow)]) == 0
    capsys.readouterr()
    assert tool.main([str(pg), "--allowlist", str(allow), "--strict-allowlist"]) == 1
    assert "matches no test any lane collected" in capsys.readouterr().out


def test_a_required_lane_without_a_ledger_fails(tmp_path, capsys):
    pg = _ledger(tmp_path / "pg.json", "postgresql", ["t.py::a"])
    assert tool.main([str(pg), "--require-lanes", "postgresql,sqlite"]) == 1
    assert "no ledger from the sqlite lane" in capsys.readouterr().out


def test_an_unreadable_ledger_is_an_error(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"version": 99}))
    assert tool.main([str(bad)]) == 2


def test_the_checked_in_allowlist_is_small_and_every_entry_has_a_reason():
    entries, problems = tool.load_allowlist(ALLOWLIST)
    assert problems == []
    assert len(entries) <= 10, "keep the allowlist small: make the test run somewhere"


@pytest.mark.parametrize("job", ["backend-tests", "second-db-test-lane"])
def test_both_lanes_write_and_upload_a_ledger_the_merge_job_reads(job):
    import yaml

    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    steps = jobs[job]["steps"]
    lane = "postgresql" if job == "backend-tests" else "sqlite"
    runs = " ".join(str(s.get("run", "")) for s in steps)
    assert "-p sandloom.core.testing.skip_ledger" in runs
    assert f"--sandloom-skip-ledger-lane={lane}" in runs
    uploads = [s for s in steps if "upload-artifact" in str(s.get("uses", ""))]
    assert any(
        str(s["with"]["name"]).startswith("skip-ledger-")
        and s["with"]["path"] == "skip-ledger/"
        and s.get("if") == "${{ always() }}"
        for s in uploads
    )
    merge = jobs["skip-ledger"]
    assert {"backend-tests", "examples", "second-db-test-lane"} <= set(merge["needs"])
    command = " ".join(str(s.get("run", "")) for s in merge["steps"])
    assert "tools/ci/skip_ledger.py" in command
    assert "--require-lanes postgresql,sqlite" in command
    assert "--allowlist tools/ci/skip_ledger_allowlist.json" in command


def test_the_examples_write_postgresql_ledgers_with_the_lanes_base():
    import yaml

    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    gate = next(
        s for s in jobs["examples"]["steps"] if "sandloom test" in str(s.get("run"))
    )
    env = gate["env"]
    assert env["PYTEST_ADDOPTS"] == "-p sandloom.core.testing.skip_ledger"
    assert env["SANDLOOM_SKIP_LEDGER_LANE"] == "postgresql"
    assert env["SANDLOOM_SKIP_LEDGER_BASE"] == "${{ github.workspace }}"
    lite = " ".join(str(s.get("run", "")) for s in jobs["second-db-test-lane"]["steps"])
    assert '--sandloom-skip-ledger-base="${{ github.workspace }}"' in lite
    assert any(
        str(s.get("with", {}).get("name", "")).startswith(
            "skip-ledger-postgresql-examples-"
        )
        for s in jobs["examples"]["steps"]
    )


#: The kernel suites the SQLite lane runs (their guarantees are stated for
#: serial writers: G-UOW-09, G-CMD-01, G-SAGA-07).
KERNEL_ON_SQLITE = (
    "src/sandloom/core/tests/test_sagas_protocol.py",
    "src/sandloom/core/tests/test_sagas_recovery.py",
    "tests/laws/test_saga_laws.py",
    "src/sandloom/core/tests/test_sqlite_nested_units.py",
    "src/sandloom/core/tests/test_nesting_policy.py",
    "tests/integration/test_durable_command_idempotency.py",
)


def test_the_sqlite_lane_runs_the_kernel_suites_with_their_postgresql_side():
    import yaml

    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    step = next(
        s
        for s in jobs["second-db-test-lane"]["steps"]
        if "skip-ledger/sqlite-kernel.json" in str(s.get("run", ""))
    )
    run = str(step["run"])
    assert "--sandloom-skip-ledger-lane=sqlite" in run
    assert '-m "not slow"' in run
    assert step["env"]["SANDLOOM_TEST_DATABASE_URL"].startswith("sqlite+aiosqlite:///")
    for path in KERNEL_ON_SQLITE:
        assert path in run.split(), path
        assert (ROOT / path).is_file(), path
    # the PostgreSQL side: backend-tests runs the whole suite (no paths), so
    # every one of these files has a ledger entry on both lanes
    full = next(
        s
        for s in jobs["backend-tests"]["steps"]
        if "--sandloom-skip-ledger-lane=postgresql" in str(s.get("run", ""))
    )
    assert not any(a.endswith(".py") or "/tests" in a for a in str(full["run"]).split())
