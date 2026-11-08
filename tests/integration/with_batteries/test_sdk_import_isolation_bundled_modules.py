"""Importing ``sandloom.sdk`` and the bundled modules is side-effect free
(Public API 1.0, spec §2.4; moved from
``tests/architecture/test_sdk_import_isolation.py``, KB-05c).

``tools/checks/import_audit.py`` imports every public ``sandloom.sdk``
module and the bundled modules under its spies (environment reads, sockets,
logging handlers, threads, ``atexit`` / signal handlers, file opens, engine /
client and ``Settings`` construction) in a ``python -I`` subprocess with an
empty environment and working directory, then runs
``help(sandloom.sdk.events)``. Nothing may be recorded. The architecture
suite keeps the check that the spies see an entry point forcing settings.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[3]
AUDIT = BACKEND / "tools" / "checks" / "import_audit.py"


def _audit(tmp_path: Path, *modules: str, help_target: str = "") -> dict[str, list]:
    args = [sys.executable, "-I", str(AUDIT), "--json", *modules]
    if help_target:
        args += ["--help-target", help_target]
    result = subprocess.run(
        args, cwd=tmp_path, env={}, capture_output=True, text=True, check=False
    )
    assert result.stdout, result.stderr[-4000:]
    report: dict[str, list] = json.loads(result.stdout)
    return report


def test_sdk_import_is_side_effect_free(tmp_path: Path) -> None:
    report = _audit(tmp_path, help_target="sandloom.sdk.events")
    assert report["errors"] == []
    assert [
        f"[{v['kind']}] {v['detail']} at {v['where']} ({v['importing']})"
        for v in report["violations"]
    ] == []
