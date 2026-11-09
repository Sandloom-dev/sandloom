"""Static metadata of access atoms, read from the generated literal (H-20).

The archcheck rules never name an atom. An atom is a class whose body carries
the ``__sandloom_atom__`` literal that ``sandloom generate`` writes beside a
consumer's contract stub (ADR-0014 decision 10); a rule resolves the name a
file imports to the file that defines it, reads that class's literal with
``ast.literal_eval`` and asks the literal: which parameter binds an
organization (``binds``), which an account (``binds`` or ``fact``), which
argument values carry the ``admin`` risk (``risk``). A third-party atom is
recognised by what it declares, an aliased or re-exported name by where it is
defined. Nothing is imported, nothing is executed, no file outside the
imported modules is read, and each file is read once per run.

The same literals let the ``handwritten-access-check`` rule (answer #22)
recognise a hand-written ``Requirement`` that calls a declared access check
through ``Contract(REF)`` and compare its tier, category and risk with the
declaration.

What it cannot see: a call through ``getattr``, a dynamically built atom, an
authority decided in a function body. Those are not proven here; the route is
classified independently and the runtime checks apply (the backstop).

``_KERNEL_ATOMS`` is the one transitional table: the kernel's own domain atoms
(``OrgRole``, ``Subscribed``, ...) still carry no literal until KB-34 removes
them and every atom is generated. It is keyed by the file that *defines* the
class, never by the name a caller imports, and is deleted with those atoms.
"""

from __future__ import annotations

import ast
import functools
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ATOM_ATTR = "__sandloom_atom__"
_MAX_REEXPORT_DEPTH = 5

AtomLiteral = dict[str, Any]

#: Kernel domain atoms without a generated literal, by (defining file's path
#: suffix, class). Removed with the atoms (KB-34).
_KERNEL_ATOMS: dict[tuple[str, str], AtomLiteral] = {
    ("sandloom/shared/authz/atoms_live.py", "OrgRole"): {
        "args": ["minimum"],
        "binds": {"organization_id": ["org", "org_id", False]},
        "risk": {"admin": {"minimum": ["admin", "owner"]}},
    },
    ("sandloom/shared/authz/atoms_live.py", "SubscriptionActive"): {
        "binds": {"tenant_id": ["account_id"]},
    },
    ("sandloom/shared/authz/atoms_live.py", "Credits"): {
        "binds": {"tenant_id": ["account_id"]},
    },
    ("sandloom/shared/authz/atoms_claims.py", "Subscribed"): {
        "binds": {"tenant_id": ["account_id"]},
    },
    ("sandloom/shared/authz/atoms_claims.py", "Feature"): {
        "binds": {"tenant_id": ["account_id"]},
    },
    ("sandloom/shared/authz/atoms_claims.py", "WithinLimit"): {
        "binds": {"tenant_id": ["account_id"]},
    },
}


def clear_caches() -> None:
    """Forget every resolved module and parsed file (tests change ``sys.path``)."""
    _module_file.cache_clear()
    _file_classes.cache_clear()


@functools.lru_cache(maxsize=1024)
def _module_file(module: str) -> Path | None:
    """The source file of an absolute module on ``sys.path`` (no import)."""
    rel = Path(*module.split("."))
    for entry in sys.path:
        base = Path(entry) if entry else Path.cwd()
        for candidate in (base / f"{rel}.py", base / rel / "__init__.py"):
            if candidate.is_file():
                return candidate
    return None


def _relative_file(origin: Path, node: ast.ImportFrom) -> Path | None:
    base = origin.parent
    for _ in range(node.level - 1):
        base = base.parent
    rel = Path(*(node.module or "").split(".")) if node.module else Path()
    for candidate in (base / f"{rel}.py", base / rel / "__init__.py"):
        if candidate.is_file() and candidate != origin:
            return candidate
    return None


def _import_file(node: ast.ImportFrom, origin: Path | None) -> Path | None:
    if node.level:
        return _relative_file(origin, node) if origin is not None else None
    return _module_file(node.module) if node.module else None


def _atom_value(cls: ast.ClassDef) -> AtomLiteral | None:
    for item in cls.body:
        target: str | None = None
        value: ast.expr | None = None
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            target, value = item.target.id, item.value
        elif (
            isinstance(item, ast.Assign)
            and len(item.targets) == 1
            and isinstance(item.targets[0], ast.Name)
        ):
            target, value = item.targets[0].id, item.value
        if target != ATOM_ATTR or value is None:
            continue
        try:
            literal = ast.literal_eval(value)
        except ValueError:
            return None
        return literal if isinstance(literal, dict) and literal else None
    return None


def _class_literals(tree: ast.AST) -> dict[str, AtomLiteral | None]:
    """``{class: literal or None}`` for each top-level class of a tree."""
    return {
        node.name: _atom_value(node)
        for node in getattr(tree, "body", ())
        if isinstance(node, ast.ClassDef)
    }


class _FileInfo:
    """What a source file defines and re-exports (parsed once)."""

    __slots__ = ("classes", "imports")

    def __init__(self, tree: ast.AST, path: Path) -> None:
        self.classes = _class_literals(tree)
        # local name -> (file the import comes from, name there)
        self.imports: dict[str, tuple[Path, str]] = {}
        for node in getattr(tree, "body", ()):
            if isinstance(node, ast.ImportFrom):
                source = _import_file(node, path)
                if source is None:
                    continue
                for alias in node.names:
                    self.imports[alias.asname or alias.name] = (source, alias.name)


@functools.lru_cache(maxsize=1024)
def _file_classes(path: Path) -> _FileInfo | None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None
    return _FileInfo(tree, path)


def _kernel_literal(path: Path, name: str) -> AtomLiteral | None:
    posix = path.as_posix()
    for (suffix, cls), literal in _KERNEL_ATOMS.items():
        if cls == name and posix.endswith(suffix):
            return literal
    return None


def _resolve(path: Path, name: str, depth: int = 0) -> AtomLiteral | None:
    """The literal of the class ``name`` defined in, or re-exported by, ``path``."""
    info = _file_classes(path)
    if info is None or depth > _MAX_REEXPORT_DEPTH:
        return None
    if name in info.classes:
        return info.classes[name] or _kernel_literal(path, name)
    origin = info.imports.get(name)
    if origin is not None:
        return _resolve(origin[0], origin[1], depth + 1)
    return None


def atom_literals(tree: ast.AST, origin: Path | None = None) -> dict[str, AtomLiteral]:
    """``{local name: literal}`` of the atoms a file defines or imports.

    A name imported from a module (``from x import Atom as A``) is keyed by its
    local name; a name reached through a module alias (``import x as m`` /
    ``from pkg import x``) is keyed ``"m.Atom"``.
    """
    found: dict[str, AtomLiteral] = {
        name: lit for name, lit in _class_literals(tree).items() if lit
    }
    for node in getattr(tree, "body", ()):
        if isinstance(node, ast.ImportFrom):
            source = _import_file(node, origin)
            for alias in node.names:
                local = alias.asname or alias.name
                literal = _resolve(source, alias.name) if source is not None else None
                if literal is not None:
                    found[local] = literal
                    continue
                # ``from pkg import module``: its atoms are reached as ``module.Atom``
                dotted = ".".join(filter(None, [node.module, alias.name]))
                sub = ast.ImportFrom(module=dotted, names=[], level=node.level)
                _add_module_atoms(found, local, sub, origin)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    sub = ast.ImportFrom(module=alias.name, names=[], level=0)
                    _add_module_atoms(found, alias.asname, sub, origin)
    return found


def _add_module_atoms(
    found: dict[str, AtomLiteral], alias: str, sub: ast.ImportFrom, origin: Path | None
) -> None:
    path = _import_file(sub, origin)
    info = _file_classes(path) if path is not None else None
    if info is None or path is None:
        return
    for cls in (*info.classes, *info.imports):
        literal = _resolve(path, cls)
        if literal is not None:
            found[f"{alias}.{cls}"] = literal


def literal_of(node: ast.AST, atoms: Mapping[str, AtomLiteral]) -> AtomLiteral | None:
    """The literal of the atom a call (or its callee) names, else ``None``."""
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return atoms.get(node.id)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return atoms.get(f"{node.value.id}.{node.attr}")
    return None


def _binds(literal: AtomLiteral) -> dict[str, Any]:
    binds = literal.get("binds")
    return binds if isinstance(binds, dict) else {}


def _kinds(literal: AtomLiteral) -> set[str]:
    return {b[0] for b in _binds(literal).values() if isinstance(b, list) and b}


def binds_org(literal: AtomLiteral) -> str | None:
    """The default organization parameter of an atom with an org bind."""
    for bind in _binds(literal).values():
        if isinstance(bind, list) and bind and bind[0] == "org":
            return str(bind[1]) if len(bind) > 1 and bind[1] else "org_id"
    return None


def binds_account(literal: AtomLiteral) -> bool:
    """An atom whose account is stated with ``of=`` (contract or claim atom)."""
    return bool(_kinds(literal) & {"account_type", "account_id"}) or "fact" in literal


def _scalar(node: ast.AST, consts: Mapping[str, ast.expr]) -> Any:
    if isinstance(node, ast.Name) and node.id in consts:
        node = consts[node.id]
    return node.value if isinstance(node, ast.Constant) else _UNKNOWN


_UNKNOWN = object()


def call_risks(
    call: ast.Call, literal: AtomLiteral, consts: Mapping[str, ast.expr] | None = None
) -> set[str]:
    """The risks an atom call carries by its declared value table (only for
    arguments the call states as constants)."""
    consts = consts or {}
    names = [a for a in literal.get("args") or [] if a not in _binds(literal)]
    params: dict[str, Any] = {}
    for name, arg in zip(names, call.args, strict=False):
        params[name] = _scalar(arg, consts)
    for kw in call.keywords:
        if kw.arg:
            params[kw.arg] = _scalar(kw.value, consts)
    return {
        risk
        for risk, conditions in (literal.get("risk") or {}).items()
        if all(params.get(p, _UNKNOWN) in values for p, values in conditions.items())
    }


def _calls(nodes: list[ast.AST]) -> list[ast.Call]:
    return [n for root in nodes for n in ast.walk(root) if isinstance(n, ast.Call)]


def admin_gate(
    nodes: list[ast.AST],
    atoms: Mapping[str, AtomLiteral],
    consts: Mapping[str, ast.expr] | None = None,
) -> bool:
    """A declared atom call with the ``admin`` risk among ``nodes``."""
    for call in _calls(nodes):
        literal = literal_of(call, atoms)
        if literal is not None and "admin" in call_risks(call, literal, consts):
            return True
    return False


def member_gate(
    nodes: list[ast.AST],
    atoms: Mapping[str, AtomLiteral],
    consts: Mapping[str, ast.expr] | None = None,
) -> bool:
    """A declared atom call that binds an organization and has no admin risk."""
    for call in _calls(nodes):
        literal = literal_of(call, atoms)
        if (
            literal is not None
            and binds_org(literal) is not None
            and "admin" not in call_risks(call, literal, consts)
        ):
            return True
    return False


# --------------------------------------------------------------------------- #
# hand-written Requirement calling a declared access check (answer #22)
# --------------------------------------------------------------------------- #
def _ref_methods(
    tree: ast.AST, origin: Path | None
) -> dict[str, dict[str, AtomLiteral]]:
    """``{ContractRef constant: {method: literal}}`` of the imported stubs that
    declare access checks."""
    out: dict[str, dict[str, AtomLiteral]] = {}
    for node in getattr(tree, "body", ()):
        if not isinstance(node, ast.ImportFrom):
            continue
        path = _import_file(node, origin)
        info = _file_classes(path) if path is not None else None
        if info is None:
            continue
        by_method = {
            str(lit["method"]): lit
            for lit in info.classes.values()
            if lit and "method" in lit and "contract" in lit
        }
        if not by_method:
            continue
        for alias in node.names:
            if alias.name.isupper():  # the ContractRef constant
                out[alias.asname or alias.name] = by_method
    return out


def _declared_clients(
    tree: ast.AST, origin: Path | None
) -> dict[str, dict[str, AtomLiteral]]:
    """``{client name: {method: literal}}`` for ``x = Contract(REF)`` where the
    stub of ``REF`` declares access checks."""
    refs = _ref_methods(tree, origin)
    clients: dict[str, dict[str, AtomLiteral]] = {}
    if not refs:
        return clients
    for node in getattr(tree, "body", ()):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and _callee(node.value) == "Contract"
            and node.value.args
            and isinstance(node.value.args[0], ast.Name)
            and node.value.args[0].id in refs
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    clients[target.id] = refs[node.value.args[0].id]
    return clients


def _callee(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


_TIERS = {"TIER_CLAIM": 1, "TIER_RESOURCE": 2, "TIER_LIVE": 3}


def _class_attr(cls: ast.ClassDef, name: str) -> Any:
    for item in cls.body:
        if (
            isinstance(item, ast.Assign)
            and len(item.targets) == 1
            and isinstance(item.targets[0], ast.Name)
            and item.targets[0].id == name
        ):
            value = item.value
            if isinstance(value, ast.Constant):
                return value.value
            if isinstance(value, ast.Name):
                return _TIERS.get(value.id, value.id)
            if isinstance(value, ast.Attribute):
                return value.attr.lower()
    return None


def _is_generated_atom(cls: ast.ClassDef) -> bool:
    return _atom_value(cls) is not None


def handwritten_access_checks(tree: ast.AST, origin: Path | None = None) -> list[str]:
    """``Line N: ...`` for each hand-written class whose body calls a declared
    access check with other metadata than its declaration: another tier or
    category, or a declared risk the class does not state (``risks()``)."""
    clients = _declared_clients(tree, origin)
    if not clients:
        return []
    out: list[str] = []
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef) or _is_generated_atom(cls):
            continue
        for node in ast.walk(cls):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.attr in clients.get(node.func.value.id, {})
            ):
                continue
            literal = clients[node.func.value.id][node.func.attr]
            where = (
                f"Line {cls.lineno}: {cls.name} calls the declared access check "
                f"{literal['contract']}.{literal['method']}"
            )
            tier = _class_attr(cls, "tier")
            if tier != literal.get("tier"):
                stated = "without stating tier" if tier is None else f"with tier {tier}"
                out.append(f"{where} {stated} (declared {literal.get('tier')})")
            category = _class_attr(cls, "category")
            if category != literal.get("category"):
                stated = (
                    "without stating category"
                    if category is None
                    else f"with category {category!r}"
                )
                out.append(f"{where} {stated} (declared {literal.get('category')!r})")
            states_risk = any(
                isinstance(item, ast.FunctionDef) and item.name == "risks"
                for item in cls.body
            )
            if literal.get("risk") and not states_risk:
                out.append(
                    f"{where} but does not state its declared risk "
                    f"{sorted(literal['risk'])} (risks())"
                )
            break
    return out


__all__ = [
    "admin_gate",
    "atom_literals",
    "binds_account",
    "binds_org",
    "call_risks",
    "clear_caches",
    "handwritten_access_checks",
    "literal_of",
    "member_gate",
]
