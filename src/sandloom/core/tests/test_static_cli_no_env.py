"""The static CLIs work with NO environment, and ``sandloom check`` explains PyYAML.

``sandloom.core/__init__`` used to build ``settings`` eagerly, so ANY
``import sandloom.core.<submodule>`` demanded DATABASE_URL / JWT_SECRET & co. — the
module generator (then ``sandloom scaffold``, now ``sandloom add``) crashed in a
fresh shell. The re-exports are now lazy (PEP 562); these tests run the
console scripts in a subprocess with an EMPTY environment, from a directory
with no ``.env``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_BIN = Path(sys.executable).parent
_BACKEND = Path(__file__).resolve().parents[4]


def _run_empty_env(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, cwd=cwd, env={}, capture_output=True, text=True, timeout=120
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["sandloom", "migrate", "--help"],
        ["sandloom", "new", "--help"],
        ["sandloom", "check", "--list-rules"],
        ["sandloom", "add", "--help"],
        ["sandloom", "add", "deploy", "--help"],
        ["sandloom", "admin", "--help"],
    ],
    ids=lambda a: " ".join(a),
)
@pytest.mark.subprocess_probe
def test_static_cli_runs_with_empty_environment(
    argv: list[str], tmp_path: Path
) -> None:
    script = _BIN / argv[0]
    if not script.exists():  # pragma: no cover - not an installed env
        pytest.skip(f"console script {argv[0]} is not installed")
    proc = _run_empty_env([str(script), *argv[1:]], tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip()


@pytest.mark.slow
@pytest.mark.subprocess_probe
def test_sandloom_check_runs_against_a_manifest_with_empty_environment(
    tmp_path: Path,
) -> None:
    """A real ``sandloom check`` run (manifest + analyzers) needs no env either."""
    proc = _run_empty_env(
        [
            str(_BIN / "sandloom"),
            "check",
            "--rules",
            str(_BACKEND / "architecture_rules.yml"),
        ],
        tmp_path,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "sandloom check: OK" in proc.stdout


@pytest.mark.subprocess_probe
def test_importing_static_submodules_does_not_build_settings(tmp_path: Path) -> None:
    code = (
        "import sys\n"
        "import sandloom.core.cli.add, sandloom.core.cli.project, sandloom.core.cli.check\n"
        "import sandloom.core.archcheck\n"
        "assert 'sandloom.core.config.settings' not in sys.modules, 'settings was imported'\n"
        "print('ok')\n"
    )
    proc = _run_empty_env([sys.executable, "-c", code], tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


def test_sandloom_check_without_pyyaml_prints_install_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from sandloom.core.cli import check

    monkeypatch.setitem(sys.modules, "yaml", None)  # import yaml -> ImportError
    rc = check.main(["--rules", str(_BACKEND / "architecture_rules.yml")])

    assert rc == 2
    err = capsys.readouterr().err
    assert "PyYAML" in err
    assert 'pip install "sandloom[check]"' in err
