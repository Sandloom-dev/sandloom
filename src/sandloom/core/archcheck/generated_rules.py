"""``generated-reserved`` (SANDLOOM-C032): a module's ``_generated/`` is the
generator's (ADR-0013 decision 1).

``<module>/_generated/`` holds the copies ``sandloom generate`` writes for the
contracts the module consumes. ``sandloom check`` refuses:

- a file in it that the generator did not write, or wrote and somebody
  edited (its provenance digest no longer matches,
  :mod:`sandloom.core.descriptors.generated_provenance`);
- an import of another module's ``_generated`` package (static, relative or a
  constant dynamic import): a module consumes a contract through its own copy.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Sequence
from pathlib import Path

from sandloom.core.archcheck.analyzers import dynamic_imports, parse_file
from sandloom.core.descriptors.generated_provenance import GENERATED, is_generated

__all__ = ["find_reserved_generated_problems"]

Hit = tuple[Path, str]


def find_reserved_generated_problems(
    module_dir: Path, package: str, files: Sequence[Path]
) -> list[Hit]:
    """The ``generated-reserved`` hits of one module: ``module_dir`` is its
    directory, ``package`` its dotted package, ``files`` the sources whose
    imports are checked (module code and tests)."""
    return [*_foreign_files(module_dir), *_foreign_imports(module_dir, package, files)]


def _foreign_files(module_dir: Path) -> Iterator[Hit]:
    root = module_dir / GENERATED
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).parts
        if not path.is_file() or "__pycache__" in rel or rel[-1].startswith("."):
            continue
        try:
            problem = is_generated(path.name, path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            problem = "not a text file sandloom generate writes"
        if problem is not None:
            yield (
                path,
                f"{module_dir.name}/{GENERATED}/{'/'.join(rel)} is reserved for "
                f"sandloom generate: {problem}",
            )


def _dotted(package: str, module_dir: Path, path: Path) -> tuple[str, bool]:
    """``(dotted name, is a package)`` of a source file of the module."""
    rel = path.relative_to(module_dir).with_suffix("").parts
    parts = [package, *rel]
    if parts[-1] == "__init__":
        return ".".join(parts[:-1]), True
    return ".".join(parts), False


def _absolute(node: ast.ImportFrom, current: str, is_package: bool) -> str:
    if not node.level:
        return node.module or ""
    base = current.split(".")
    keep = len(base) - node.level + (1 if is_package else 0)
    parts = base[: max(keep, 0)]
    return ".".join([*parts, node.module] if node.module else parts)


def _targets(node: ast.AST, current: str, is_package: bool) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        base = _absolute(node, current, is_package)
        return [base, *(f"{base}.{alias.name}" for alias in node.names)]
    return []


def _foreign_owner(target: str, package: str) -> str | None:
    """The package whose ``_generated`` ``target`` reaches, when not ours."""
    parts = target.split(".")
    if GENERATED not in parts:
        return None
    owner = ".".join(parts[: parts.index(GENERATED)])
    return None if owner == package else owner


def _foreign_imports(
    module_dir: Path, package: str, files: Sequence[Path]
) -> Iterator[Hit]:
    for path in files:
        try:
            tree = parse_file(path)
        except SyntaxError:
            continue
        current, is_package = _dotted(package, module_dir, path)
        found: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            for target in _targets(node, current, is_package):
                owner = _foreign_owner(target, package)
                if owner is not None:
                    found.append((getattr(node, "lineno", 0), owner))
                    break
        found += [
            (line, owner)
            for line, name, _ in dynamic_imports(tree)
            if (owner := _foreign_owner(name, package)) is not None
        ]
        for line, owner in sorted(set(found)):
            yield (
                path,
                f"Line {line}: imports {owner}.{GENERATED}, another module's "
                "generated copies: declare the contract in this module's "
                "consumes (Contract(REF) at module level, or define_module("
                "consumes=[...])) and import its own copy after sandloom generate",
            )
