"""Checkout-owned navigation metadata, never implementation text or imports."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

from sandloom.core.cli.inspection import files
from sandloom.core.cli.inspection.model import Doc
from sandloom.core.cli.inspection.read import Reader


def _record(reader: Reader, path: str) -> tuple[Doc, ast.Module | None]:
    text = files.read(reader.root, path)
    parse_error = None
    try:
        tree = ast.parse(text, filename=path) if path.endswith(".py") else None
    except SyntaxError as exc:
        tree = None
        parse_error = f"Syntax metadata unavailable at line {exc.lineno}"
    description = (
        ast.get_docstring(tree)
        if tree
        else (text.strip().split("\n", 1)[0] if path.endswith(".md") else "")
    )
    if Path(path).name in {"AGENTS.md", "CLAUDE.md"}:
        description = ""
    summary = " ".join((description or "").splitlines()[:1])
    symbols: list[Doc] = (
        [
            {
                "name": node.name,
                "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                "line": node.lineno,
            }
            for node in tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        if tree
        else []
    )
    return {
        "path": path,
        "bytes": len(text.encode("utf-8")),
        "summary": summary[:140] + ("…" if len(summary) > 140 else ""),
        "symbols": sorted(
            symbols,
            key=lambda s: (s["name"].startswith("_"), s["kind"] != "class", s["line"]),
        )[:4],
        "more_symbols": max(0, len(symbols) - 4),
        **({"parse_error": parse_error} if parse_error else {}),
    }, tree


def _revision(tree: ast.Module | None) -> tuple[str | None, list[str]]:
    values: dict[str, object] = {}
    for node in tree.body if tree else []:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        target = node.targets[0] if isinstance(node, ast.Assign) else node.target
        if (
            isinstance(target, ast.Name)
            and target.id in {"revision", "down_revision"}
            and node.value is not None
        ):
            try:
                values[target.id] = ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError):
                continue
    parent = values.get("down_revision")
    parents = (
        [parent]
        if isinstance(parent, str)
        else list(parent)
        if isinstance(parent, tuple) and all(isinstance(p, str) for p in parent)
        else []
    )
    revision = values.get("revision")
    return revision if isinstance(revision, str) else None, parents


def build(reader: Reader, module: Doc, candidates: list[str]) -> Doc:
    root = Path(module["source"])
    listed: list[Doc] = []
    grouped: dict[str, list[str]] = {}
    revisions, parents = set(), set()
    for path in candidates:
        relative = Path(path).relative_to(root)
        record, tree = _record(reader, path)
        group = relative.parts[0] if len(relative.parts) > 1 else ""
        if group not in {"tests", "migrations", "schemas", "models"}:
            listed.append(record)
            continue
        grouped.setdefault(group, []).append(path)
        if group == "migrations":
            revision, links = _revision(tree)
            if revision:
                revisions.add(revision)
                parents.update(links)
    groups = []
    for name, paths in sorted(grouped.items()):
        entry: Doc = {
            "path": (root / name).as_posix(),
            "kind": name,
            "count": len(paths),
        }
        if name == "tests":
            entry["directories"] = dict(
                sorted(
                    Counter(
                        str(Path(p).parent.relative_to(root / name)) for p in paths
                    ).items()
                )
            )
        elif name == "migrations":
            entry.update(
                revision_count=len(revisions), heads=sorted(revisions - parents)
            )
        else:
            entry["files"] = [
                Path(p).relative_to(root / name).as_posix() for p in paths
            ]
        groups.append(entry)
    return {"files": listed, "file_groups": groups, "file_count": len(candidates)}
