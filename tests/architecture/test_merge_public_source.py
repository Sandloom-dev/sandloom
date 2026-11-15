"""The public-source.json merge driver merges path sets, not lines."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tools.packaging import merge_public_source as driver

ROOT = Path(__file__).resolve().parents[2]


def _policy(*paths: str, excl: dict | None = None) -> dict:
    return {
        "schema": 1,
        "include": [{"reason": "Framework.", "paths": list(paths)}],
        "exclude_files": excl or {},
    }


def test_two_branches_adding_beside_one_sibling_both_land() -> None:
    base = _policy("src/a/x.py", "src/b/y.py")
    ours = _policy("src/a/x.py", "src/a/x2.py", "src/b/y.py")
    theirs = _policy("src/a/x.py", "src/a/x1.py", "src/b/y.py")
    merged = driver.merge(base, ours, theirs)
    assert merged["include"][0]["paths"] == [
        "src/a/x.py",
        "src/a/x1.py",
        "src/a/x2.py",
        "src/b/y.py",
    ]


def test_a_path_removed_on_one_side_goes() -> None:
    base = _policy("src/a/x.py", "src/a/old.py")
    ours = _policy("src/a/x.py", "src/a/old.py", "src/a/new.py")
    theirs = _policy("src/a/x.py")
    assert driver.merge(base, ours, theirs)["include"][0]["paths"] == [
        "src/a/x.py",
        "src/a/new.py",
    ]


def test_a_path_both_sides_added_appears_once() -> None:
    base = _policy("src/a/x.py")
    both = _policy("src/a/x.py", "src/a/y.py")
    assert driver.merge(base, both, both)["include"][0]["paths"] == [
        "src/a/x.py",
        "src/a/y.py",
    ]


def test_a_group_one_side_added_is_kept() -> None:
    base = _policy("src/a/x.py")
    theirs = _policy("src/a/x.py")
    theirs["include"].append({"reason": "New.", "paths": ["new/z.md"]})
    merged = driver.merge(base, _policy("src/a/x.py"), theirs)
    assert [g["reason"] for g in merged["include"]] == ["Framework.", "New."]


def test_mapping_keys_merge_and_stay_sorted() -> None:
    base = _policy("a", excl={"b": "r"})
    ours = _policy("a", excl={"a": "r", "b": "r"})
    theirs = _policy("a", excl={"b": "r", "c": "r"})
    assert driver.merge(base, ours, theirs)["exclude_files"] == {
        "a": "r",
        "b": "r",
        "c": "r",
    }


def test_the_same_key_changed_differently_is_a_conflict() -> None:
    base = _policy("a", excl={"b": "r"})
    with pytest.raises(driver.Conflict):
        driver.merge(
            base, _policy("a", excl={"b": "one"}), _policy("a", excl={"b": "two"})
        )


def test_a_removed_group_changed_on_the_other_side_is_a_conflict() -> None:
    base = _policy("src/a/x.py")
    theirs = {**_policy(), "include": []}
    with pytest.raises(driver.Conflict):
        driver.merge(base, _policy("src/a/x.py", "src/a/y.py"), theirs)


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_git_merges_parallel_additions_without_a_conflict(tmp_path: Path) -> None:
    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )

    policy = tmp_path / "tools/packaging/public-source.json"
    policy.parent.mkdir(parents=True)
    shutil.copy(ROOT / "tools/packaging/merge_public_source.py", policy.parent)
    shutil.copy(ROOT / ".gitattributes", tmp_path / ".gitattributes")

    def write(p: dict) -> None:
        policy.write_text(json.dumps(p, indent=2) + "\n")

    git("init", "-q", "-b", "main")
    driver_cmd = f"{sys.executable} tools/packaging/merge_public_source.py %O %A %B"
    git("config", "merge.public-source.driver", driver_cmd)
    write(_policy("src/a/x.py", "src/b/y.py"))
    git("add", "-A")
    git("commit", "-qm", "base")
    git("checkout", "-qb", "side")
    write(_policy("src/a/x.py", "src/a/x1.py", "src/b/y.py"))
    git("commit", "-qam", "side")
    git("checkout", "-q", "main")
    write(_policy("src/a/x.py", "src/a/x2.py", "src/b/y.py"))
    git("commit", "-qam", "main")
    result = git("merge", "-q", "--no-edit", "side")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(policy.read_text())["include"][0]["paths"] == [
        "src/a/x.py",
        "src/a/x1.py",
        "src/a/x2.py",
        "src/b/y.py",
    ]
