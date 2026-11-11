"""tools/ci/no_batteries.py: the kernel-without-batteries job's import hook and list.

The CI job (ADR-0012 fitness test, KB-05) is only as good as its hook: an
installed hook must hide every bundled module package from a fresh
interpreter, even with the checkout's ``src/`` on ``sys.path``. The pending
list is a ratchet (KB-05c): it never grows past ``CEILING`` (empty, so the
list stays empty), an entry whose test passes without the batteries fails as
a strict xfail, and an entry whose test or whole file is gone is stale.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tools.ci import no_batteries

REPO = Path(__file__).resolve().parents[2]


def test_the_installed_hook_hides_every_battery_from_a_child_interpreter(
    tmp_path: Path,
) -> None:
    """A ``.pth`` hook in a site directory: no battery importable from ``src/``."""
    site = tmp_path / "site"
    site.mkdir()
    no_batteries.install(site)
    probe = (
        "import site, sys, importlib.util; "
        f"site.addsitedir({str(site)!r}); "
        f"sys.path.insert(0, {str(REPO / 'src')!r}); "
        "print(sorted(m for m in "
        f"{sorted(no_batteries.BATTERIES)!r} if importlib.util.find_spec(m))); "
        "import sandloom.core; print('kernel importable')"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["[]", "kernel importable"], done.stdout


def test_every_pending_entry_names_an_existing_test_file() -> None:
    """Each entry is a ``path::test`` id whose file is in the checkout."""
    for test, waits_for in no_batteries.load_pending().items():
        path, sep, name = test.partition("::")
        assert sep and name.startswith("test_") and "[" not in name, test
        assert (REPO / path).is_file(), test
        assert waits_for, test


def test_the_pending_list_never_grows_past_its_ceiling() -> None:
    """The list only goes down: every entry is in ``CEILING``, and the
    ceiling holds nothing the list dropped. Both are empty since KB-05c."""
    pending = set(no_batteries.load_pending())
    assert no_batteries.above_ceiling(no_batteries.load_pending()) == []
    assert set(no_batteries.CEILING) == pending
    assert no_batteries.CEILING == frozenset()


def test_a_new_entry_is_above_the_ceiling() -> None:
    entry = "src/sandloom/core/tests/test_x.py::test_needs_identity"
    assert no_batteries.above_ceiling({entry: "KB-xx"}) == [entry]
    assert no_batteries.above_ceiling({entry: "KB-xx"}, frozenset({entry})) == []


def test_a_new_entry_refuses_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """The kernel job's plugin refuses a list that grew, before collection."""
    entry = "src/sandloom/core/tests/test_x.py::test_needs_identity"
    monkeypatch.setattr(no_batteries, "importable_batteries", lambda: [])
    monkeypatch.setattr(no_batteries, "load_pending", lambda: {entry: "KB-xx"})
    with pytest.raises(pytest.UsageError, match="only goes down") as refused:
        no_batteries.pytest_configure(SimpleNamespace())
    assert entry in str(refused.value)


def test_stale_entries_whole_file_deleted_or_test_gone(tmp_path: Path) -> None:
    """A deleted file is stale even though nothing of it was collected; a
    collected file without the test is stale; an uncollected existing file
    is not judged (a partial run)."""
    (tmp_path / "kept.py").write_text("")
    (tmp_path / "other.py").write_text("")
    pending = {
        "gone.py::test_a": "x",
        "kept.py::test_moved": "x",
        "kept.py::test_here": "x",
        "other.py::test_not_collected": "x",
    }
    nodeids = ["kept.py::test_here[p1]", "kept.py::test_unrelated"]
    assert no_batteries.stale_entries(pending, nodeids, tmp_path) == [
        "gone.py::test_a",
        "kept.py::test_moved",
    ]


class _Item:
    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid
        self.markers: list[Any] = []

    def add_marker(self, marker: Any) -> None:
        self.markers.append(marker)


def test_a_pending_test_runs_as_a_strict_xfail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pending test is run, not deselected: passing without the batteries
    is ``XPASS(strict)``, a failure that names the entry."""
    (tmp_path / "t.py").write_text("")
    monkeypatch.setattr(no_batteries, "load_pending", lambda: {"t.py::test_a": "KB-xx"})
    items = [_Item("t.py::test_a[1]"), _Item("t.py::test_b")]
    config = SimpleNamespace(rootpath=tmp_path)
    no_batteries.pytest_collection_modifyitems(config, items)
    assert [i.nodeid for i in items] == ["t.py::test_a[1]", "t.py::test_b"]
    (marker,) = items[0].markers
    assert marker.mark.name == "xfail" and marker.mark.kwargs["strict"] is True
    assert "KB-xx" in marker.mark.kwargs["reason"]
    assert items[1].markers == []


def test_a_stale_entry_refuses_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(no_batteries, "load_pending", lambda: {"gone.py::test_a": "x"})
    with pytest.raises(pytest.UsageError, match="gone.py::test_a"):
        no_batteries.pytest_collection_modifyitems(
            SimpleNamespace(rootpath=tmp_path), [_Item("t.py::test_b")]
        )


def test_a_passing_pending_test_fails_the_run(tmp_path: Path) -> None:
    """End to end in a child pytest: the plugin turns a pending test that
    passes into a failure and one that fails into an xfail."""
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_probe.py").write_text(
        "def test_passes_now():\n"
        "    pass\n\n\n"
        "def test_still_needs_a_battery():\n"
        "    import sandloom_identity_not_installed\n"
    )
    pending = tmp_path / "pending.json"
    pending.write_text(
        '{"waits": ["test_probe.py::test_passes_now", '
        '"test_probe.py::test_still_needs_a_battery"]}'
    )
    (tmp_path / "conftest.py").write_text(
        "from pathlib import Path\n"
        "from tools.ci import no_batteries\n"
        f"_PENDING = Path({str(pending)!r})\n"
        "_load = no_batteries.load_pending\n"
        "no_batteries.importable_batteries = lambda: []\n"
        "no_batteries.load_pending = lambda path=_PENDING: _load(path)\n"
        "no_batteries.CEILING = frozenset(no_batteries.load_pending())\n"
        "pytest_configure = no_batteries.pytest_configure\n"
        "pytest_collection_modifyitems = (\n"
        "    no_batteries.pytest_collection_modifyitems\n"
        ")\n"
    )
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-rX", "-q"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(REPO)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 1, done.stdout + done.stderr
    assert "1 failed, 1 xfailed" in done.stdout, done.stdout
    assert "XPASS(strict)" in done.stdout and "no_batteries_pending" in done.stdout
