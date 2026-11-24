"""The DX eval's tier and skills metrics and its repeated runs.

``benchmarks/dx/tiermetrics.py`` reads one run's transcript and diff;
``benchmarks/dx/run_eval.py`` stores them, runs a task ``--runs N`` times and
reports each metric's mean and spread. Runs here on synthetic transcripts: no
agent, no Docker. The wave-1 label's stored results are checked for the metrics
a ``recompute`` over its transcripts wrote.
"""

from __future__ import annotations

import argparse
import importlib
import json
from dataclasses import asdict
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
DX = ROOT / "benchmarks" / "dx"

GREEN = "  PASS             suite (not slow)   20.5s\n  PASS             mypy  0.7s\n"
RED = "  FAIL (exit 1)    notes   20.8s\n  PASS             mypy  0.7s\nsandloom test: FAILED (notes)\n"
T009_LINE = (
    "teamhub/modules/x/services.py:3: Line 3: DbSession from sandloom.sdk is not in "
    "the beginner tier — use a beginner name, or mark the line "
    "`# sandloom: advanced <reason>` (SANDLOOM-T009)\n"
)
AGENTS_MENTION = (
    "an advanced import carries `# sandloom: advanced <reason>` (SANDLOOM-T009).\n"
)
TIER = {
    "sandloom.sdk": {"Event", "Sandloom"},
    "sandloom.sdk.testing": {"client"},
}


@pytest.fixture
def tm(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(DX))
    return importlib.import_module("tiermetrics")


@pytest.fixture
def run_eval(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(DX))
    return importlib.import_module("run_eval")


def write_transcript(path: Path, steps: list[tuple[str, dict[str, Any], str]]) -> None:
    """One assistant message (one tool_use) and its tool_result per step."""
    lines = []
    for i, (tool, inp, out) in enumerate(steps):
        lines.append(
            {
                "type": "assistant",
                "message": {
                    "id": f"msg_{i}",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"tu_{i}",
                            "name": tool,
                            "input": inp,
                        }
                    ],
                },
            }
        )
        lines.append(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": f"tu_{i}",
                            "content": out,
                        }
                    ],
                },
            }
        )
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def bash(cmd: str, out: str = "") -> tuple[str, dict[str, Any], str]:
    return ("Bash", {"command": cmd}, out)


def test_transcript_metrics_count_docs_recipes_skills_and_the_first_green_test(
    tm: ModuleType, tmp_path: Path
) -> None:
    steps = [
        bash("uv run sandloom docs recipes 2>&1 | head -60"),
        bash("uv run sandloom docs recipe org-gate"),
        bash("uv run sandloom docs billing.credits.v1 | head; sandloom docs WallClock"),
        bash("uv run sandloom docs --write"),
        bash("uv run sandloom test --help", GREEN),
        bash("uv run sandloom test | grep -E FAILED | head", ""),
        bash("uv run sandloom test", RED),
        bash("cat AGENTS.md", AGENTS_MENTION),
        bash("uv run sandloom test", T009_LINE + RED),
        bash("uv run sandloom test", GREEN),  # step 9: the first green one
        bash("uv run sandloom test", GREEN),
        (
            "Read",
            {"file_path": "/x/.agents/skills/sandloom-event-version-roll/SKILL.md"},
            "",
        ),
        bash("cat .agents/skills/sandloom-new-module/SKILL.md"),
        bash("ls .agents/skills/*/SKILL.md"),
        ("Skill", {"skill": "sandloom-split-deploy"}, ""),
        bash("uv run sandloom docs -h"),  # help is no lookup
        bash("uv run sandloom docs recipe --help"),
    ]
    path = tmp_path / "transcript.jsonl"
    write_transcript(path, steps)
    got = tm.transcript_metrics(path)
    assert got == {
        "docs_lookups": 2,
        "docs_names": ["WallClock", "billing.credits.v1"],
        "recipes_read": ["org-gate"],
        "recipe_index_views": 1,
        "skills_read": [
            "sandloom-event-version-roll",
            "sandloom-new-module",
            "sandloom-split-deploy",
        ],
        "first_green_turn": 10,
        "t009_failures": 1,
    }


def test_a_message_streamed_in_blocks_is_one_turn(
    tm: ModuleType, tmp_path: Path
) -> None:
    """``num_turns`` counts messages: a text block and a tool_use block of one
    message id are one turn."""
    text = {"type": "text", "text": "running the tests"}
    call = {
        "type": "tool_use",
        "id": "tu",
        "name": "Bash",
        "input": {"command": "sandloom test"},
    }
    result = {"type": "tool_result", "tool_use_id": "tu", "content": GREEN}
    rows = [
        {"type": "assistant", "message": {"id": "m1", "content": [text]}},
        {"type": "assistant", "message": {"id": "m1", "content": [call]}},
        {"type": "user", "message": {"content": [result]}},
    ]
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows))
    assert tm.transcript_metrics(path)["first_green_turn"] == 1


def test_no_green_test_is_none_and_an_error_result_is_not_green(
    tm: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "transcript.jsonl"
    write_transcript(path, [bash("uv run sandloom test", RED)])
    assert tm.transcript_metrics(path)["first_green_turn"] is None
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[1]["message"]["content"][0] |= {"content": GREEN, "is_error": True}
    path.write_text("\n".join(json.dumps(r) for r in rows))
    assert tm.transcript_metrics(path)["first_green_turn"] is None


def test_a_missing_transcript_raises(tm: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        tm.transcript_metrics(tmp_path / "transcript.jsonl")


PATCH = """\
diff --git a/app/modules/x/services.py b/app/modules/x/services.py
new file mode 100644
--- /dev/null
+++ b/app/modules/x/services.py
@@ -0,0 +1,8 @@
+from sandloom.sdk import Event, DbSession
+from sandloom.sdk import (  # sandloom: advanced chain
+    Sandloom,
+    WallClock,
+)
+import sandloom.sdk.migrations
+from sandloom.sdk.migrations import run_module_migrations  # sandloom: advanced gate
+from sandloom.core import something
diff --git a/app/modules/x/tests/test_x.py b/app/modules/x/tests/test_x.py
new file mode 100644
--- /dev/null
+++ b/app/modules/x/tests/test_x.py
@@ -0,0 +1,2 @@
+from sandloom.sdk.testing import client, make_user as mk
+from sandloom.sdk import InTestsOnly  # sandloom: advanced test double
diff --git a/app/modules/x/_generated/contracts/y.py b/app/modules/x/_generated/contracts/y.py
new file mode 100644
--- /dev/null
+++ b/app/modules/x/_generated/contracts/y.py
@@ -0,0 +1 @@
+from sandloom.sdk.authz import Generated  # sandloom: advanced generated code
diff --git a/app/shared/contracts/z.py b/app/shared/contracts/z.py
new file mode 100644
--- /dev/null
+++ b/app/shared/contracts/z.py
@@ -0,0 +1 @@
+from sandloom.sdk import InSharedContracts
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -0,0 +1 @@
+from sandloom.sdk import InAMarkdownFile
diff --git a/app/modules/x/old.py b/app/modules/x/old.py
--- a/app/modules/x/old.py
+++ /dev/null
@@ -1 +0,0 @@
-from sandloom.sdk import Removed
"""


def test_diff_metrics_find_names_outside_the_tier_and_markers(tm: ModuleType) -> None:
    got = tm.diff_metrics(PATCH, TIER)
    assert got == EXPECTED


EXPECTED = {
    # the module's own code only: not tests, generated code, shared contracts, docs
    "advanced_markers": 2,
    "sdk_names": [
        "sandloom.sdk.DbSession",
        "sandloom.sdk.Event",
        "sandloom.sdk.Sandloom",
        "sandloom.sdk.WallClock",
        "sandloom.sdk.migrations",
        "sandloom.sdk.migrations.run_module_migrations",
    ],
    "tier_misses": [
        "sandloom.sdk.DbSession",
        "sandloom.sdk.WallClock",
        "sandloom.sdk.migrations",
        "sandloom.sdk.migrations.run_module_migrations",
    ],
    "test_sdk_names": [
        "sandloom.sdk.InTestsOnly",
        "sandloom.sdk.testing.client",
        "sandloom.sdk.testing.make_user",
    ],
    "test_tier_misses": ["sandloom.sdk.InTestsOnly", "sandloom.sdk.testing.make_user"],
}


def test_the_patch_appmode_writes_gives_the_same_metrics(
    tm: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """App mode's ``changes.patch`` is ``difflib`` output (``appmode.app_patch``):
    ``---``/``+++`` headers and no ``diff --git`` line. Every real run's diff
    metrics were 0 until a file started at its ``+++`` header."""
    monkeypatch.syspath_prepend(str(DX))
    appmode = importlib.import_module("appmode")
    pristine, app = tmp_path / "pristine", tmp_path / "app"
    files = {
        "app/modules/x/services.py": (
            "from sandloom.sdk import Event\n",
            "from sandloom.sdk import Event, DbSession\n"
            "x = 1\n"
            "from sandloom.sdk import (  # sandloom: advanced chain\n"
            "    Sandloom,\n"
            "    WallClock,\n"
            ")\n",
        ),
        "app/modules/x/tests/test_x.py": ("", "from sandloom.sdk import OnlyInTests\n"),
        "app/modules/x/_generated/g.py": ("", "from sandloom.sdk import Generated\n"),
    }
    for rel, (old, new) in files.items():
        for root, text in ((pristine, old), (app, new)):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
    patch = appmode.app_patch(app, pristine, sorted(files))
    assert "diff --git" not in patch
    got = tm.diff_metrics(patch, TIER)
    assert got["advanced_markers"] == 1
    assert got["sdk_names"] == [
        "sandloom.sdk.DbSession",
        "sandloom.sdk.Event",
        "sandloom.sdk.Sandloom",
        "sandloom.sdk.WallClock",
    ]
    assert got["tier_misses"] == ["sandloom.sdk.DbSession", "sandloom.sdk.WallClock"]
    assert got["test_sdk_names"] == ["sandloom.sdk.OnlyInTests"]


def test_a_changed_line_that_looks_like_a_header_does_not_start_a_file(
    tm: ModuleType,
) -> None:
    patch = (
        "--- a/m/modules/x/a.py\n+++ b/m/modules/x/a.py\n@@ -1,2 +1,3 @@\n"
        "-++ old\n"
        "+++ b/other/shared/b.py\n"  # an added line "++ b/other/…" inside the hunk
        "+from sandloom.sdk import Late\n"
        " context\n"
    )
    # read as a header it would hand the import to shared/b.py, which is not counted
    assert tm.diff_metrics(patch, TIER)["sdk_names"] == ["sandloom.sdk.Late"]


def test_an_empty_diff_has_no_names(tm: ModuleType) -> None:
    assert tm.diff_metrics("", TIER) == {
        "advanced_markers": 0,
        "sdk_names": [],
        "tier_misses": [],
        "test_sdk_names": [],
        "test_tier_misses": [],
    }


def test_the_real_tier_lists_beginner_names_and_not_advanced_ones(
    tm: ModuleType,
) -> None:
    tier = tm.beginner_tier()
    assert tier["sandloom.sdk"], "the tier read nothing"
    assert "DbSession" not in tier["sandloom.sdk"]
    assert tm.diff_metrics(PATCH, tier)["tier_misses"]


def test_a_run_yields_exactly_the_result_fields_of_the_metrics(
    tm: ModuleType, run_eval: ModuleType, tmp_path: Path
) -> None:
    write_transcript(tmp_path / "transcript.jsonl", [bash("sandloom test", GREEN)])
    got = tm.run_metrics(tmp_path / "transcript.jsonl", tmp_path / "none.patch", TIER)
    assert set(got) == set(tm.METRIC_KEYS)  # a missing patch is an empty diff
    assert set(tm.METRIC_KEYS) <= set(run_eval.Result.__dataclass_fields__)


def row(**over: Any) -> dict[str, Any]:
    """A stored result row of a passing task with the metrics' defaults."""
    run_eval = importlib.import_module("run_eval")
    base = run_eval.Result(
        task="t1_notes",
        label="L",
        ref="",
        model="m",
        outcome="pass",
        tokens_total=1000,
        turns=10,
        tool_calls=10,
        first_green_turn=5,
    )
    return asdict(base) | over


def test_spread_is_mean_sample_sd_min_and_max(run_eval: ModuleType) -> None:
    assert run_eval.spread([10.0, 20.0, 30.0]) == {
        "n": 3,
        "mean": 20.0,
        "sd": 10.0,
        "min": 10.0,
        "max": 30.0,
    }
    assert run_eval.spread([7.0])["sd"] is None
    assert run_eval.spread([])["mean"] is None


def test_aggregate_leaves_out_errored_runs_and_missing_metrics(
    run_eval: ModuleType,
) -> None:
    rows = [
        row(docs_lookups=1, first_green_turn=4),
        row(docs_lookups=3, first_green_turn=None),  # never green: not a 0
        row(docs_lookups=8, tokens_total=0, outcome="error"),  # errored
        # a result stored before the metrics existed
        {k: v for k, v in row().items() if k not in run_eval.tiermetrics.METRIC_KEYS},
    ]
    got = run_eval.aggregate(rows)["t1_notes"]
    assert got["runs"] == 4
    assert got["errored"] == 1
    assert got["docs_lookups"]["n"] == 2
    assert got["docs_lookups"]["mean"] == 2.0
    assert got["first_green_turn"]["n"] == 1
    assert got["first_green_turn"]["mean"] == 4.0
    summary = run_eval.summarize(rows)  # an old row must not break the headline
    assert summary["tier_metrics_runs"] == 2
    assert summary["mean_docs_lookups"] == 2.0


def test_select_tasks_and_runs(run_eval: ModuleType) -> None:
    assert run_eval.select_tasks(None) is None
    assert run_eval.select_tasks("t1,t3") == ["t1", "t3"]
    measured = run_eval.select_tasks("measured")
    assert measured == ["t1", "t3", "t5", "t9", "t10", "t6", "t8", "t7"]
    ids = {t.id.split("_")[0] for t in run_eval.load_tasks(measured, "tasks", "app")}
    assert ids == set(measured)  # every measured name selects a task

    def runs(tasks: str | None, n: int | None) -> int:
        return run_eval.resolve_runs(argparse.Namespace(tasks=tasks, runs=n))

    assert runs(None, None) == 1
    assert runs("t1", None) == 1
    assert runs("measured", None) == 3
    assert runs("measured", 5) == 5
    with pytest.raises(SystemExit):
        runs("t1", 0)


def test_for_run_gives_each_run_its_own_label_and_one_run_keeps_it(
    run_eval: ModuleType,
) -> None:
    args = argparse.Namespace(label="L", model="m")
    one = run_eval.for_run(args, 1, 1)
    assert (one.label, one.results_label, one.run) == ("L", "", 1)
    labels = [run_eval.for_run(args, k, 3) for k in (1, 2, 3)]
    assert [a.label for a in labels] == ["L.r1", "L.r2", "L.r3"]
    assert {a.results_label for a in labels} == {"L"}
    assert [a.run for a in labels] == [1, 2, 3]
    assert args.label == "L"  # the caller's args are untouched
    assert run_eval.result_stem("t1_notes", 2, "L") == "t1_notes.r2"
    assert run_eval.result_stem("t1_notes", 1, "") == "t1_notes"


def stored_runs(run_eval: ModuleType, tmp_path: Path, docs: list[int]) -> Path:
    """Run ``write_result`` for one task's runs; ``docs[k]`` lookups in run k+1."""
    results = tmp_path / "results"
    assert run_eval.RESULTS_DIR == results, "patch RESULTS_DIR first"
    for k, n in enumerate(docs, start=1):
        out = tmp_path / f"home-L.r{k}" / "t1_notes"
        out.mkdir(parents=True)
        steps = [bash(f"uv run sandloom docs Name{i}") for i in range(n)]
        write_transcript(
            out / "transcript.jsonl", [*steps, bash("sandloom test", GREEN)]
        )
        (out / "changes.patch").write_text(PATCH)
        result = run_eval.Result(
            task="t1_notes",
            label=f"L.r{k}",
            ref="",
            model="m",
            outcome="pass",
            tokens_total=1000 * k,
            turns=len(steps) + 1,
            run=k,
            group="L",
        )
        run_eval.write_result(result, out)
    return results


def test_runs_are_stored_per_run_and_reported_with_mean_and_spread(
    run_eval: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_eval, "RESULTS_DIR", tmp_path / "results")
    results = stored_runs(run_eval, tmp_path, [1, 2, 3])
    assert sorted(p.name for p in (results / "L").glob("*.json")) == [
        "t1_notes.r1.json",
        "t1_notes.r2.json",
        "t1_notes.r3.json",
    ]
    stored = json.loads((results / "L" / "t1_notes.r3.json").read_text())
    assert stored["docs_lookups"] == 3
    assert stored["first_green_turn"] == 4
    assert stored["tier_misses"]
    run_eval.cmd_report(argparse.Namespace(label="L", compare=None))
    summary = (results / "L" / "summary.md").read_text()
    assert "t1_notes r2" in summary
    assert "mean ±sd (min-max)" in summary
    assert "| t1_notes | 3 | 3/3 |" in summary
    assert "2 ±1 (1-3)" in summary  # docs lookups over the three runs
    per_task = json.loads((results / "L" / "summary.json").read_text())["per_task"]
    assert per_task["t1_notes"]["docs_lookups"]["sd"] == 1.0
    assert per_task["t1_notes"]["runs"] == 3


def test_one_run_per_task_says_a_sample_is_not_a_difference(
    run_eval: ModuleType,
) -> None:
    text = run_eval.runs_section("L", [row()])
    assert "one sample cannot separate a change" in text


def test_the_first_green_mean_shows_how_many_runs_it_averages(
    run_eval: ModuleType,
) -> None:
    rows = [
        row(first_green_turn=4),
        row(first_green_turn=None),
        row(first_green_turn=6),
    ]
    assert "5 ±1.4 (4-6) n=2/3" in run_eval.runs_section("L", rows)


def test_totals_over_no_metric_rows_are_unknown_not_zero(run_eval: ModuleType) -> None:
    old = {k: v for k, v in row().items() if k not in run_eval.tiermetrics.METRIC_KEYS}
    summary = run_eval.summarize([old])
    assert summary["t009_failures_total"] is None
    assert summary["advanced_markers_total"] is None
    assert run_eval.summarize([row(t009_failures=2)])["t009_failures_total"] == 2


def test_promote_keeps_every_run_of_a_repeated_label(
    run_eval: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_eval, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(run_eval, "HERE", tmp_path)
    stored_runs(run_eval, tmp_path, [1, 2, 3])
    run_eval.cmd_promote(argparse.Namespace(label="L"))
    tasks = json.loads((tmp_path / "baseline.json").read_text())["tasks"]
    assert sorted(tasks) == ["t1_notes.r1", "t1_notes.r2", "t1_notes.r3"]


def test_rescore_refuses_a_label_of_repeated_runs(
    run_eval: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_eval, "RESULTS_DIR", tmp_path / "results")
    stored_runs(run_eval, tmp_path, [1, 2])
    args = argparse.Namespace(
        label="L", tasks=None, suite="tasks", checks_only=False, hidden_only=False
    )
    with pytest.raises(SystemExit, match="repeated runs"):
        run_eval.cmd_rescore(args)


def test_recompute_adds_the_metrics_to_a_stored_result(
    run_eval: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = tmp_path / "results"
    monkeypatch.setattr(run_eval, "RESULTS_DIR", results)
    monkeypatch.setattr(run_eval, "eval_home", lambda: tmp_path / "home")
    out = tmp_path / "home" / "W" / "t1_notes"
    out.mkdir(parents=True)
    write_transcript(
        out / "transcript.jsonl",
        [bash("uv run sandloom docs recipe org-gate"), bash("sandloom test", GREEN)],
    )
    (out / "changes.patch").write_text(PATCH)
    old = asdict(run_eval.Result(task="t1_notes", label="W", ref="", model="m"))
    for key in run_eval.tiermetrics.METRIC_KEYS:
        del old[key]
    (results / "W").mkdir(parents=True)
    (results / "W" / "t1_notes.json").write_text(json.dumps(old))
    run_eval.cmd_recompute(argparse.Namespace(label="W"))
    new = json.loads((results / "W" / "t1_notes.json").read_text())
    assert new["recipes_read"] == ["org-gate"]
    assert new["first_green_turn"] == 2
    assert new["tier_misses"]


def test_wave1_results_carry_the_metrics() -> None:
    """``run_eval.py recompute --label dx-wave1-2026-11-24`` wrote them from the
    kept transcripts; every stored task has them."""
    stored = sorted((DX / "results" / "dx-wave1-2026-11-24").glob("t[0-9]*.json"))
    assert len(stored) == 10
    for path in stored:
        data = json.loads(path.read_text())
        missing = [
            k
            for k in (
                "docs_lookups",
                "recipes_read",
                "skills_read",
                "first_green_turn",
                "t009_failures",
                "tier_misses",
            )
            if k not in data
        ]
        assert not missing, f"{path.name} lacks {missing}"
    t7 = json.loads(
        (DX / "results/dx-wave1-2026-11-24/t7_credits_event_v2.json").read_text()
    )
    assert t7["skills_read"] == ["sandloom-event-version-roll"]  # the one skill read
