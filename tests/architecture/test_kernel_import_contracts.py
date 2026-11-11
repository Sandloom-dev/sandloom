"""The kernel import contracts of ADR-0007 hold (import-linter).

``.importlinter`` declares the layers of ``sandloom.core`` (exhaustive: a
new kernel package fails until it is placed), acyclic packages at every
depth, and configuration as a leaf. import-linter builds the import graph
with grimp — function-local imports included, ``TYPE_CHECKING`` blocks
excluded — so a cycle cannot hide behind a lazy import.

Every ``ignore_imports`` entry is tolerated debt: it must carry a written
``; reason:`` comment above it (one comment may cover a group of entries),
and import-linter fails an entry that no longer matches an import.

``kernel-imports-no-module`` proves invariant I-02 of
``design/kernel-boundary/requirements.md`` for static imports: the kernel
(``sandloom.core``, ``sdk``, ``shared``, ``main``) imports no business-module
package. External packages are in the graph (``include_external_packages``),
the contract names every discovered ``sandloom_*`` package, and a planted
import proves the contract breaks.
"""

from __future__ import annotations

import configparser
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_shared_purity import business_modules

BACKEND = Path(__file__).resolve().parents[2]
CONFIG = BACKEND / ".importlinter"


KERNEL_MODULE_CONTRACT = "importlinter:contract:kernel-imports-no-module"


def _lint_imports(config: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    lint_imports = Path(sys.executable).with_name("lint-imports")
    return subprocess.run(
        [str(lint_imports), "--config", str(config), "--no-cache"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _config() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.read(CONFIG, encoding="utf-8")
    return parser


def test_kernel_import_contracts_are_kept() -> None:
    result = _lint_imports(CONFIG, BACKEND / "src")
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


def _lines(value: str) -> set[str]:
    return {line.strip() for line in value.splitlines() if line.strip()}


def test_the_kernel_module_contract_names_every_business_module() -> None:
    """I-02: every ``sandloom_*`` package (src layout, ``sandloom.modules``
    entry points) is forbidden to the whole kernel, and external packages
    are in the graph (without them the contract would check nothing)."""
    config = _config()
    assert config["importlinter"].getboolean("include_external_packages")
    contract = config[KERNEL_MODULE_CONTRACT]
    assert contract["type"] == "forbidden"
    assert _lines(contract["source_modules"]) == {
        "sandloom.core",
        "sandloom.sdk",
        "sandloom.shared",
        "sandloom.main",
    }
    assert "ignore_imports" not in contract
    missing = business_modules() - _lines(contract["forbidden_modules"])
    assert not missing, f"add to {KERNEL_MODULE_CONTRACT}: {sorted(missing)}"


def test_the_kernel_module_contract_breaks_on_a_planted_import(
    tmp_path: Path,
) -> None:
    """A function-local kernel import of a module package breaks the real
    contract text (I-02), run on a minimal tree."""
    tree = tmp_path / "tree"
    for package in ("sandloom", "sandloom/core", "sandloom/sdk", "sandloom/shared"):
        (tree / package).mkdir(parents=True)
        (tree / package / "__init__.py").write_text("")
    (tree / "sandloom" / "main.py").write_text("")
    (tree / "sandloom" / "core" / "bad.py").write_text(
        "def f():\n    from sandloom_billing import api\n"
    )
    real = _config()
    planted = configparser.ConfigParser()
    planted["importlinter"] = dict(real["importlinter"])
    planted[KERNEL_MODULE_CONTRACT] = dict(real[KERNEL_MODULE_CONTRACT])
    config = tmp_path / "importlinter.ini"
    with config.open("w") as handle:
        planted.write(handle)
    result = _lint_imports(config, tree)
    assert result.returncode != 0, result.stdout
    assert "sandloom.core.bad -> sandloom_billing" in result.stdout
