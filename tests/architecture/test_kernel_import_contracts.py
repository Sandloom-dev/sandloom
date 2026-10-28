"""The kernel import contracts of ADR-0007 hold (import-linter).

``.importlinter`` declares the layers of ``sandloom.core`` (exhaustive: a
new kernel package fails until it is placed), acyclic packages at every
depth, and configuration as a leaf. import-linter builds the import graph
with grimp — function-local imports included, ``TYPE_CHECKING`` blocks
excluded — so a cycle cannot hide behind a lazy import.

Every ``ignore_imports`` entry is tolerated debt: it must carry a written
``; reason:`` comment above it (one comment may cover a group of entries),
and import-linter fails an entry that no longer matches an import.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
CONFIG = BACKEND / ".importlinter"


def test_kernel_import_contracts_are_kept() -> None:
    lint_imports = Path(sys.executable).with_name("lint-imports")
    result = subprocess.run(
        [str(lint_imports), "--config", str(CONFIG), "--no-cache"],
        cwd=BACKEND / "src",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-2000:]
    assert "0 broken" in result.stdout


def test_every_ignored_import_has_a_written_reason() -> None:
    missing: list[str] = []
    in_ignores = False
    reason = False
    for raw in CONFIG.read_text().splitlines():
        line = raw.strip()
        if raw.startswith("ignore_imports"):
            in_ignores, reason = True, False
            continue
        if not raw.startswith((" ", "\t")):
            in_ignores = False
            continue
        if not in_ignores or not line:
            continue
        if line.startswith((";", "#")):
            if "reason:" in line:
                reason = True
            continue
        if "->" in line and not reason:
            missing.append(line)
    assert missing == [], f"ignore_imports entries without a reason: {missing}"
