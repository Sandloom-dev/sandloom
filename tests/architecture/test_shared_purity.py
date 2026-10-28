"""`sandloom.shared` must stay module-agnostic.

`shared` is the one layer every business module may import, so it must never
import a business module back — otherwise it becomes a transitive coupling
channel (module A -> shared -> module B). The cross-module boundary test covers
the business modules themselves; this closes the gap that nothing checked
`shared/` for the same violation.
"""

from __future__ import annotations

import ast
from pathlib import Path

# .../tests/architecture/test_shared_purity.py -> .../src/sandloom
_SANDLOOM = Path(__file__).resolve().parents[2] / "src" / "sandloom"
_SHARED = _SANDLOOM / "shared"


def _business_modules() -> set[str]:
    """Every business-module package (discovered, not hardcoded).

    A NEW module added under ``src/sandloom/`` is automatically in scope — the old
    hardcoded ``{identity, tenancy, billing}`` silently exempted any future
    module, so ``shared`` could import it with no test failure.
    """
    return {
        d.name
        for d in _SANDLOOM.iterdir()
        if d.is_dir()
        and (d / "__init__.py").exists()
        and not d.name.startswith(("_", "."))
        and d.name not in ("core", "shared", "sdk")
    }


# Business-module packages shared must never import (anything not core/shared).
_BUSINESS_MODULES = _business_modules()
_FORBIDDEN_PREFIXES = tuple(f"sandloom.{m}" for m in _BUSINESS_MODULES)


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    # The file's package (its __package__), e.g. ('sandloom', 'shared', 'authz'),
    # used to resolve relative imports to absolute names so a laundered
    # ``from ..identity import X`` is not invisible.
    package = path.relative_to(_SANDLOOM.parent).with_suffix("").parts[:-1]
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = list(package)[: len(package) - (node.level - 1)]
                if node.module:
                    anchor += node.module.split(".")
                if anchor:
                    names.append(".".join(anchor))
            elif node.module:
                names.append(node.module)
    return names


def test_shared_does_not_import_business_modules() -> None:
    violations: list[str] = []
    for py in _SHARED.rglob("*.py"):
        for imported in _imported_modules(py):
            if imported in _FORBIDDEN_PREFIXES or imported.startswith(
                tuple(p + "." for p in _FORBIDDEN_PREFIXES)
            ):
                rel = py.relative_to(_SANDLOOM.parent)
                violations.append(f"{rel}: imports {imported}")
    assert not violations, (
        "sandloom.shared must not import business modules:\n" + "\n".join(violations)
    )
