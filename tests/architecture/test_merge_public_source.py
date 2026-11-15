"""The public-source.json merge driver merges path sets, not lines."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
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
    base = _policy("src/x.py", excl={"b": "r"})
    ours = _policy("src/x.py", excl={"a": "r", "b": "r"})
    theirs = _policy("src/x.py", excl={"b": "r", "c": "r"})
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


def test_a_path_we_removed_stays_removed() -> None:
    base = _policy("src/a/x.py", "src/a/old.py")
    ours = _policy("src/a/x.py")
    theirs = _policy("src/a/x.py", "src/a/old.py", "src/a/new.py")
    assert driver.merge(base, ours, theirs)["include"][0]["paths"] == [
        "src/a/new.py",
        "src/a/x.py",
    ]


def test_a_path_landing_in_two_groups_is_a_conflict() -> None:
    base = _policy("src/a/x.py")
    base["include"].append({"reason": "Other.", "paths": ["docs/a.md"]})
    ours = copy.deepcopy(base)
    ours["include"][0]["paths"].append("src/a/y.py")
    theirs = copy.deepcopy(base)
    theirs["include"][1]["paths"].append("src/a/y.py")
    with pytest.raises(driver.Conflict, match="two include groups"):
        driver.merge(base, ours, theirs)


def test_a_path_both_included_and_excluded_is_a_conflict() -> None:
    base = _policy("src/a/x.py")
    ours = _policy("src/a/x.py", "src/a/y.py")
    theirs = _policy("src/a/x.py", excl={"src/a/y.py": "generated"})
    with pytest.raises(driver.Conflict, match="included and excluded"):
        driver.merge(base, ours, theirs)


def _files(tmp_path: Path, base: str, ours: str, theirs: str) -> list[str]:
    names = []
    for name, text in (("base", base), ("ours", ours), ("theirs", theirs)):
        (tmp_path / name).write_text(text)
        names.append(str(tmp_path / name))
    return names


def _dump(p: dict) -> str:
    return json.dumps(p, indent=2) + "\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_conflict_leaves_markers_not_a_plain_copy_of_ours(tmp_path: Path) -> None:
    base, ours, theirs = _files(
        tmp_path,
        _dump(_policy("a", excl={"b": "r"})),
        _dump(_policy("a", excl={"b": "one"})),
        _dump(_policy("a", excl={"b": "two"})),
    )
    assert driver.main([base, ours, theirs]) != 0
    text = Path(ours).read_text()
    assert "<<<<<<< ours" in text
    assert '"b": "two"' in text


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_side_that_is_not_json_is_a_conflict(tmp_path: Path) -> None:
    good = _dump(_policy("a"))
    base, ours, theirs = _files(tmp_path, good, good, good + "{broken\n")
    assert driver.main([base, ours, theirs]) != 0
    assert "{broken" in Path(ours).read_text()


def _git_merge(tmp_path: Path, ours: dict, theirs: dict, *, script: bool):
    """Merge branch ``side`` (theirs) into ``main`` (ours) through the
    installed driver command; the policy file path and the merge result."""

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
    if script:
        shutil.copy(ROOT / driver.SCRIPT, policy.parent)
    shutil.copy(ROOT / ".gitattributes", tmp_path / ".gitattributes")
    git("init", "-q", "-b", "main")
    git("config", "merge.public-source.driver", driver.COMMAND)
    policy.write_text(_dump(_policy("src/a/x.py", "src/b/y.py", excl={"k": "r"})))
    git("add", "-A")
    git("commit", "-qm", "base")
    git("checkout", "-qb", "side")
    policy.write_text(_dump(theirs))
    git("commit", "-qam", "side")
    git("checkout", "-q", "main")
    policy.write_text(_dump(ours))
    git("commit", "-qam", "main")
    return policy, git("merge", "-q", "--no-edit", "side")


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("python3") is None,
    reason="needs git and python3",
)
def test_git_merges_parallel_additions_without_a_conflict(tmp_path: Path) -> None:
    policy, result = _git_merge(
        tmp_path,
        _policy("src/a/x.py", "src/a/x2.py", "src/b/y.py", excl={"k": "r"}),
        _policy("src/a/x.py", "src/a/x1.py", "src/b/y.py", excl={"k": "r"}),
        script=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(policy.read_text())["include"][0]["paths"] == [
        "src/a/x.py",
        "src/a/x1.py",
        "src/a/x2.py",
        "src/b/y.py",
    ]


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_without_the_script_git_still_leaves_markers(tmp_path: Path) -> None:
    policy, result = _git_merge(
        tmp_path,
        _policy("src/a/x.py", "src/b/y.py", excl={"k": "one"}),
        _policy("src/a/x.py", "src/b/y.py", excl={"k": "two"}),
        script=False,
    )
    assert result.returncode != 0
    assert "<<<<<<< ours" in policy.read_text()
