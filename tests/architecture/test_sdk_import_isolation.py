"""Importing ``sandloom.sdk`` is side-effect free (Public API 1.0, spec §2.4).

In a subprocess with ``python -I``, an empty environment and an empty
working directory (no ``.env``), ``tools/checks/import_audit.py`` installs spies
on environment reads, sockets, logging handlers, threads, ``atexit`` /
signal handlers, file opens, engine / client construction and ``Settings``
construction, then imports every public ``sandloom.sdk`` module and runs
``help(sandloom.sdk.events)``. Nothing may be recorded. The same audit over
the bundled modules too is in
``tests/integration/with_batteries/test_sdk_import_isolation_bundled_modules.py``.

Settings are lazy (``sandloom.core.config.settings``): entry points force them,
so a bad environment still fails at startup (the second test).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tools.checks.import_audit import DEFAULT_MODULES

BACKEND = Path(__file__).resolve().parents[2]
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


def test_the_public_sdk_import_is_side_effect_free(tmp_path: Path) -> None:
    """The audit's public ``sandloom.*`` modules alone, so the kernel suite
    runs it where no bundled module is installed (ADR-0012)."""
    kernel = [m for m in DEFAULT_MODULES if m.startswith("sandloom.")]
    assert "sandloom.sdk" in kernel
    report = _audit(tmp_path, *kernel, help_target="sandloom.sdk.events")
    assert report["errors"] == []
    assert [
        f"[{v['kind']}] {v['detail']} at {v['where']} ({v['importing']})"
        for v in report["violations"]
    ] == []


def test_the_audit_sees_an_entry_point_forcing_settings(tmp_path: Path) -> None:
    """The spies work: the composition root builds the app at import, which
    validates the (empty) environment and exits."""
    report = _audit(tmp_path, "sandloom.main")
    kinds = {v["kind"] for v in report["violations"]}
    assert "settings" in kinds
    assert any("Invalid configuration" in e for e in report["errors"])
