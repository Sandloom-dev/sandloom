"""tools/ci/no_batteries.py: the kernel-without-batteries job's import hook and list.

The CI job (ADR-0012 fitness test, KB-05) is only as good as its hook: an
installed hook must hide every bundled module package from a fresh
interpreter, even with the checkout's ``src/`` on ``sys.path``, and the
pending list may only name test files that exist.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

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
    pending = no_batteries.load_pending()
    assert pending
    for test, waits_for in pending.items():
        path, sep, name = test.partition("::")
        assert sep and name.startswith("test_") and "[" not in name, test
        assert (REPO / path).is_file(), test
        assert waits_for, test
