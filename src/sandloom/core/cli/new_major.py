"""``sandloom generate --new-major <name>``: the next major of a released
contract or event, beside the old one (ADR-0017 decision 4).

A breaking change of a released contract or event is a new ``.v(N+1)`` served
beside ``.vN``. This writes that copy: the declaration of ``<name>`` in the
module's source (a ``@contract("m.x.v1")`` class, an ``Event`` class with
``channel="m.x.v1"``) is copied under the next major, with the class renamed,
and left to be edited. The old declaration stays untouched, so both are
served. Nothing is read from git: the snapshot files say what is released.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sandloom.core.descriptors.release_marks import (
    ReleaseMarkError,
    old_marks,
)

#: ``<module>.<name>...vN``: the module is the first segment.
_NAME = re.compile(r"^(?P<base>[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)\.v(?P<major>[0-9]+)$")
#: Directories of a module that hold no declaration.
_SKIP = {"tests", "migrations", "_generated", "__pycache__"}


class NewMajorError(ValueError):
    """The request cannot be served; the message says what to do instead."""


@dataclass(frozen=True)
class _Site:
    path: Path
    node: ast.ClassDef
    kind: str  # "contract" or "event"


def _read(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def _literal(node: ast.expr) -> str | None:
    return (
        node.value
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        else None
    )


def _declared(node: ast.ClassDef) -> list[tuple[str, str]]:
    """``[(kind, name)]`` of a class: ``@contract("n")`` and ``channel="n"``."""
    found: list[tuple[str, str]] = []
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call) or not decorator.args:
            continue
        func = decorator.func
        called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        name = _literal(decorator.args[0])
        if called == "contract" and name is not None:
            found.append(("contract", name))
    for keyword in node.keywords:
        name = _literal(keyword.value)
        if keyword.arg == "channel" and name is not None:
            found.append(("event", name))
    return found


def _sites(module_dir: Path, name: str) -> list[_Site]:
    """Every module-level class of the module that declares ``name``."""
    sites: list[_Site] = []
    for path in sorted(module_dir.rglob("*.py")):
        if _SKIP & set(path.relative_to(module_dir).parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                sites += [
                    _Site(path, node, kind)
                    for kind, declared in _declared(node)
                    if declared == name
                ]
    return sites


def _snapshot_state(module_dir: Path, name: str) -> tuple[str, bool] | None:
    """``(kind, released)`` of ``name`` in the module's snapshot files, or
    ``None`` when neither lists it."""
    for kind, file, key in (
        ("contract", "contracts.snapshot.json", "contracts"),
        ("event", "module.snapshot.json", "events"),
    ):
        document = _read(module_dir / file)
        entries = (document or {}).get(key) or {}
        if name not in entries:
            continue
        try:
            marks = old_marks(document, entries, where=file)
        except ReleaseMarkError as exc:
            raise NewMajorError(str(exc)) from exc
        return kind, name in marks
    return None


def _class_of(old: str, following: str) -> str:
    """``Quota`` or ``QuotaV1`` for ``m.quota.v2``: ``QuotaV2``."""
    return re.sub(r"V\d+$", "", old) + "V" + following.rsplit(".v", 1)[1]


def _renamed(source: str, node: ast.ClassDef, name: str, following: str) -> str:
    """The declaration of ``node`` as the next major's."""
    first = min([node.lineno] + [d.lineno for d in node.decorator_list])
    lines = source.splitlines()[first - 1 : node.end_lineno]
    text = re.sub(re.escape(name) + r"(?![\w.])", following, "\n".join(lines))
    return re.sub(rf"\b{re.escape(node.name)}\b", _class_of(node.name, following), text)


def new_major(module_dir: Path, name: str) -> list[Path]:
    """Copy the declaration of the released ``name`` to the next major, in the
    file that declares it; returns the files written. Raises
    :class:`NewMajorError` for a draft (edit it), an unknown name, an existing
    next major or a declaration that is not in exactly one place."""
    match = _NAME.fullmatch(name)
    if match is None:
        raise NewMajorError(
            f"{name!r} is not a versioned contract or event name such as "
            "tenancy.membership.v1"
        )
    following = f"{match['base']}.v{int(match['major']) + 1}"
    state = _snapshot_state(module_dir, name)
    if state is None:
        raise NewMajorError(
            f"{module_dir.name} has no contract or event {name} in its snapshots: "
            "run `sandloom generate` first, or check the name"
        )
    kind, released = state
    if not released:
        raise NewMajorError(
            f"{name} is a draft: edit it in place, nothing is released to protect "
            "(`sandloom release` marks it released)"
        )
    if _snapshot_state(module_dir, following) is not None or _sites(
        module_dir, following
    ):
        raise NewMajorError(
            f"{following} exists already: edit it, it is the next major"
        )
    sites = [s for s in _sites(module_dir, name) if s.kind == kind]
    if len(sites) != 1:
        where = ", ".join(str(s.path.relative_to(module_dir)) for s in sites)
        raise NewMajorError(
            f"{name} must be declared by one module-level class to be copied; "
            + (f"found {where}" if sites else "none was found in the source")
            + f": write {following} by hand"
        )
    [site] = sites
    source = site.path.read_text(encoding="utf-8")
    copy = _renamed(source, site.node, name, following)
    taken = {n.name for n in ast.parse(source).body if isinstance(n, ast.ClassDef)}
    new_class = _class_of(site.node.name, following)
    if new_class in taken:
        raise NewMajorError(
            f"{site.path.name} already has a class {new_class}: rename it, then repeat"
        )
    lines = source.splitlines()
    end = site.node.end_lineno or len(lines)
    merged = [*lines[:end], "", "", *copy.splitlines(), *lines[end:]]
    site.path.write_text("\n".join(merged) + "\n", encoding="utf-8")
    return [site.path]


__all__ = ["NewMajorError", "new_major"]
