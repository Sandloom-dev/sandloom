"""The seeded shuffle plugin (``-p sandloom.core.testing.shuffle --sandloom-shuffle=SEED``).

The isolation gate runs the suite in a random order with a printed seed, so a
test that passes only after another one fails there, and the seed replays it.
Each case collects a tiny project in a real ``pytest`` subprocess.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_FILES = {
    f"test_{name}.py": "".join(f"def test_{name}_{i}(): pass\n" for i in range(6))
    for name in ("a", "b", "c", "d")
}


def _project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = ["-p", "no:cacheprovider"]\n'
    )
    for name, body in _FILES.items():
        (root / name).write_text(body)
    return root


def _collect(root: Path, *args: str) -> tuple[list[str], str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:xdist",
            "-p",
            "sandloom.core.testing.shuffle",
            "--collect-only",
            "-q",
            *args,
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    ids = [line for line in proc.stdout.splitlines() if "::" in line]
    return ids, proc.stdout


@pytest.mark.subprocess_probe
def test_without_the_option_the_order_is_unchanged(tmp_path: Path) -> None:
    root = _project(tmp_path / "p")
    ids, out = _collect(root)
    assert ids == sorted(ids, key=lambda i: (i.split("::")[0], int(i[-1])))
    assert "sandloom-shuffle" not in out


@pytest.mark.subprocess_probe
def test_a_seed_fixes_the_order_and_is_printed_for_replay(tmp_path: Path) -> None:
    root = _project(tmp_path / "p")
    first, out = _collect(root, "--sandloom-shuffle=1234")
    again, _ = _collect(root, "--sandloom-shuffle=1234")
    other, _ = _collect(root, "--sandloom-shuffle=99")
    assert first == again
    assert first != other
    assert sorted(first) == sorted(other)
    assert "sandloom-shuffle: seed 1234 (replay: --sandloom-shuffle=1234)" in out


@pytest.mark.subprocess_probe
def test_files_stay_contiguous_and_their_tests_are_shuffled(tmp_path: Path) -> None:
    root = _project(tmp_path / "p")
    ids, _ = _collect(root, "--sandloom-shuffle=7")
    files = [i.split("::")[0] for i in ids]
    # each file's tests are one block (module fixtures are set up once) ...
    blocks = [f for n, f in enumerate(files) if n == 0 or files[n - 1] != f]
    assert sorted(blocks) == sorted(_FILES)
    # ... and inside the blocks the definition order is gone
    assert ids != sorted(ids, key=lambda i: (blocks.index(i.split("::")[0]), i))


@pytest.mark.subprocess_probe
def test_random_picks_a_fresh_seed_and_prints_it(tmp_path: Path) -> None:
    root = _project(tmp_path / "p")
    ids, out = _collect(root, "--sandloom-shuffle=random")
    match = re.search(r"sandloom-shuffle: seed (\d+)", out)
    assert match is not None, out
    replay, _ = _collect(root, f"--sandloom-shuffle={match.group(1)}")
    assert replay == ids


@pytest.mark.subprocess_probe
def test_the_shards_of_one_seed_split_whole_files_and_cover_the_run(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path / "p")
    whole, _ = _collect(root, "--sandloom-shuffle=5")
    parts = []
    for index in (1, 2, 3):
        ids, out = _collect(root, "--sandloom-shuffle=5", f"--sandloom-shard={index}/3")
        assert f"sandloom-shuffle: shard {index}/3 ran " in out
        assert f"replay: --sandloom-shuffle=5 --sandloom-shard={index}/3" in out, out
        # the shard keeps the shuffled run's order of what it runs
        assert ids == [i for i in whole if i in ids]
        parts.append(ids)
    assert sorted(i for part in parts for i in part) == sorted(whole)
    files = [{i.split("::")[0] for i in part} for part in parts]
    assert all(files) and not (files[0] & files[1] or files[1] & files[2])
    # another seed deals the files differently
    others = [
        {
            i.split("::")[0]
            for i in _collect(
                root, f"--sandloom-shuffle={seed}", "--sandloom-shard=1/2"
            )[0]
        }
        for seed in range(1, 8)
    ]
    assert len({frozenset(f) for f in others}) > 1


@pytest.mark.subprocess_probe
@pytest.mark.parametrize(
    "args", [["--sandloom-shard=1/2"], ["--sandloom-shuffle=1", "--sandloom-shard=3/2"]]
)
def test_a_shard_needs_a_seed_and_a_valid_part(tmp_path: Path, args: list[str]) -> None:
    root = _project(tmp_path / "p")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:xdist",
            "-p",
            "sandloom.core.testing.shuffle",
            "--collect-only",
            "-q",
            *args,
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 4, proc.stdout + proc.stderr
    assert "--sandloom-shard" in proc.stderr
