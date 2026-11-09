"""``tools/agent_registry.py`` makes the routine registry edits agents used to do by hand.

``classify`` puts a new file into ``public-source.json`` beside its sibling,
``next-id`` names the next error code or guarantee id (and bumps ``issued:``),
``edges-summary`` recounts the design register's ``summary:``. Each is checked on a
small copy of its file, and against the real one where that changes nothing.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]


def _tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "agent_registry", REPO / "tools" / "agent_registry.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ar = _tool()


def _policy(root: Path) -> dict[str, Any]:
    policy: dict[str, Any] = json.loads(
        (root / ar.PUBLIC_SOURCE).read_text(encoding="utf-8")
    )
    return policy


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "tools" / "packaging").mkdir(parents=True)
    (tmp_path / ar.PUBLIC_SOURCE).write_text(
        json.dumps(
            {
                "schema": 1,
                "include": [
                    {"reason": "Docs.", "paths": ["docs/a.md", "docs/c.md"]},
                    {
                        "reason": "Tests.",
                        "paths": [
                            "tests/architecture/test_a.py",
                            "tests/unit/test_z.py",
                        ],
                    },
                ],
                "exclude_files": {"b.txt": "local"},
                "exclude_trees": {"benchmarks/": "local"},
                "symlinks": {},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "sandloom" / "core").mkdir(parents=True)
    (tmp_path / ar.ERROR_CODES).write_text(
        '    _entry(\n        "SANDLOOM-C034",\n    _entry(\n        "SANDLOOM-C009",\n        "SANDLOOM-T017",\n',
        encoding="utf-8",
    )
    (tmp_path / ar.GUARANTEES).write_text(
        "schema: 1\nguarantees: []\nissued:\n  C-AUTH: 9\n  G-AUTHZ: 13\n",
        encoding="utf-8",
    )
    (tmp_path / "design" / "kernel-boundary").mkdir(parents=True)
    (tmp_path / ar.EDGES).write_text(
        "summary:\n"
        "  total: 3  # the judged edges\n"
        "  kind: {runtime: 3}\n"
        "  current_status: {open: 2, fixed: 1}\n"
        "  target: {fixed: 3}\n"
        "  by_adr: {ADR-0012: 3}\n"
        "\n"
        "edges:\n"
        "  - {id: B-1, current_status: fixed, target: fixed, adr: ADR-0012}\n"
        "  - {id: B-2, current_status: fixed, target: fixed, adr: ADR-0012}\n"
        "  - {id: B-3, current_status: retained, target: fixed, adr: ADR-0012}\n",
        encoding="utf-8",
    )
    return tmp_path


def test_a_new_file_goes_beside_its_nearest_sibling(repo: Path) -> None:
    report = ar.classify(repo, ["tests/architecture/test_b.py", "docs/b.md"], None)
    policy = _policy(repo)
    assert policy["include"][1]["paths"] == [
        "tests/architecture/test_a.py",
        "tests/architecture/test_b.py",
        "tests/unit/test_z.py",
    ]
    assert policy["include"][0]["paths"] == ["docs/a.md", "docs/b.md", "docs/c.md"]
    assert report[0].startswith(
        "tests/architecture/test_b.py: include, beside tests/architecture/test_a.py"
    )


def test_a_classified_path_is_left_alone(repo: Path) -> None:
    before = (repo / ar.PUBLIC_SOURCE).read_text(encoding="utf-8")
    report = ar.classify(repo, ["docs/a.md", "b.txt", "benchmarks/run.py"], None)
    assert all("already classified" in line for line in report)
    assert (repo / ar.PUBLIC_SOURCE).read_text(encoding="utf-8") == before


def test_exclude_file_takes_the_reason(repo: Path) -> None:
    ar.classify(repo, ["a.txt"], "owner-local note")
    assert list(_policy(repo)["exclude_files"].items()) == [
        ("a.txt", "owner-local note"),
        ("b.txt", "local"),
    ]


def test_the_real_policy_round_trips_unchanged(tmp_path: Path) -> None:
    (tmp_path / "tools" / "packaging").mkdir(parents=True)
    real = (REPO / ar.PUBLIC_SOURCE).read_text(encoding="utf-8")
    (tmp_path / ar.PUBLIC_SOURCE).write_text(real, encoding="utf-8")
    some = json.loads(real)["include"][0]["paths"][0]
    ar.classify(tmp_path, [some], None)
    assert (tmp_path / ar.PUBLIC_SOURCE).read_text(encoding="utf-8") == real


def test_next_error_code_follows_the_highest_of_its_kind(repo: Path) -> None:
    assert ar.next_error_code(repo, "SANDLOOM-C") == "SANDLOOM-C035"
    assert ar.next_error_code(repo, "SANDLOOM-T") == "SANDLOOM-T018"


def test_next_guarantee_reads_and_bumps_issued(repo: Path) -> None:
    assert ar.next_guarantee(repo, "G-AUTHZ", bump=False)[0] == "G-AUTHZ-14"
    assert "G-AUTHZ: 13" in (repo / ar.GUARANTEES).read_text(encoding="utf-8")
    ident, note = ar.next_guarantee(repo, "G-AUTHZ", bump=True)
    assert (ident, note) == ("G-AUTHZ-14", "issued: G-AUTHZ 13 -> 14 (written)")
    assert "  G-AUTHZ: 14\n" in (repo / ar.GUARANTEES).read_text(encoding="utf-8")
    assert ar.next_guarantee(repo, "C-NEW", bump=False)[0] == "C-NEW-01"


def test_edges_summary_recounts_and_keeps_the_layout(repo: Path) -> None:
    changes = ar.edges_summary(repo, write=True)
    assert changes == ["current_status: {open: 2, fixed: 1} -> {fixed: 2, retained: 1}"]
    text = (repo / ar.EDGES).read_text(encoding="utf-8")
    assert "  current_status: {fixed: 2, retained: 1}\n" in text
    assert "  total: 3  # the judged edges\n" in text
    assert ar.edges_summary(repo, write=False) == []


def test_the_real_edges_summary_is_current() -> None:
    assert ar.edges_summary(REPO, write=False) == []


def test_the_real_next_ids_resolve() -> None:
    assert ar.next_error_code(REPO, "SANDLOOM-C").startswith("SANDLOOM-C")
    assert ar.next_guarantee(REPO, "G-TENANT", bump=False)[0].startswith("G-TENANT-")
