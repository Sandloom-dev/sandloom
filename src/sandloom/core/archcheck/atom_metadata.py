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

A name means what its last binding in the file says, as at run time: a class
defined after an import of the same name shadows the import, and so does an
import after a class (``from x import *`` included, honouring ``__all__``).

The same literals let the ``handwritten-access-check`` rule (answer #22)
recognise a hand-written class that calls a declared access check through a
contract client and compare its tier, category and risk with the declaration.
A client is any ``Contract(ref)`` / ``OptionalContract(ref)`` (aliased or
not, its ``.client()`` included) wherever it is built: at module level, in a
function or a class, inline, under an annotation, bound to an attribute,
returned by a function, or imported from a file of the same package. ``ref``
is resolved to the statement that binds ``ContractRef(...)``, however it
reaches the file: by name in any case, through a module alias, a re-export, a
star import or a local alias. The declared checks are read from the generated
stub of that contract; only a class written by ``sandloom generate`` into a
``_generated/`` package is exempt. A call through a bound method, a constant
``getattr`` or a helper function (of the file, or imported from the same
package) counts as the class's own call.

What it cannot see: a dynamically built atom, client or name (``getattr``
with a computed name, ``importlib``, ``globals()``), and an authority decided
outside the class and its package. Those are not proven here; the route is
classified independently and the runtime checks apply (the backstop).

The kernel keeps no table of atoms (KB-34): a class without the literal is
not an atom here, whatever its name, so renaming or re-exporting a class never
borrows another atom's metadata.
"""

from __future__ import annotations

import ast
import functools
import sys
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

ATOM_ATTR = "__sandloom_atom__"
_MAX_REEXPORT_DEPTH = 5
_MAX_STUB_ANCESTORS = 8
_GENERATED_DIR = "_generated"
#: the ``sandloom.sdk`` calls that build a contract client from a ContractRef
_CLIENT_FACTORIES = frozenset({"Contract", "OptionalContract"})
_FACTORY_NAMES = frozenset({"ContractRef", *_CLIENT_FACTORIES})

AtomLiteral = dict[str, Any]
#: What a name is bound to, by kind: ``("class", literal | None)``,
#: ``("func", name)``, ``("ref", contract | None)`` (``ContractRef(...)``),
#: ``("import", file | None, name)``, ``("module", file | None, dotted)``
#: (``import x as m``, ``from pkg import module``), ``("package", file,
#: dotted)`` (a bare ``import a.b``), ``("alias", expr)``, ``("other", name)``.
_Binding = tuple[Any, ...]
_Resolved = tuple[Path | None, _Binding]
_MODULE_KINDS = ("module", "package")


def clear_caches() -> None:
    """Forget every resolved module and parsed file (tests change ``sys.path``)."""
    _module_file.cache_clear()
    _file_classes.cache_clear()
    _declared_methods.cache_clear()
    _CLIENTS.clear()


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


def _callee(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


# --------------------------------------------------------------------------- #
# what a file binds, and what a name resolves to
# --------------------------------------------------------------------------- #
_BRANCHES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.TryStar,
)


def _module_statements(body: Iterable[ast.stmt]) -> Iterator[ast.stmt]:
    """A file's top-level statements in source order, through the branches of
    ``if`` / ``try`` / ``with`` / loops (they bind module names too)."""
    for node in body:
        yield node
        if isinstance(node, _BRANCHES):
            for field in ("body", "orelse", "finalbody"):
                yield from _module_statements(getattr(node, field, ()))
            for handler in getattr(node, "handlers", ()):
                yield from _module_statements(handler.body)


def _string_list(value: ast.expr) -> set[str] | None:
    try:
        literal = ast.literal_eval(value)
    except ValueError:
        return None
    if isinstance(literal, list | tuple) and all(isinstance(x, str) for x in literal):
        return set(literal)
    return None


class _FileInfo:
    """What a source file binds (parsed once).

    ``everywhere=False`` reads the module namespace in source order (the last
    binding of a name wins). ``everywhere=True`` reads every statement of the
    tree, function and class bodies included, and keeps every binding of a
    name: the C034 analysis asks whether *any* binding makes a name a
    ContractRef or a client.
    """

    __slots__ = ("bindings", "everywhere", "exports", "factories", "path", "stars")

    def __init__(
        self, tree: ast.AST, path: Path | None, *, everywhere: bool = False
    ) -> None:
        self.path = path
        self.everywhere = everywhere
        statements: list[ast.stmt] = (
            [n for n in ast.walk(tree) if isinstance(n, ast.stmt)]
            if everywhere
            else list(_module_statements(getattr(tree, "body", ())))
        )
        # local name -> sdk factory it imports (``from sdk import Contract as C``)
        self.factories: dict[str, str] = {
            alias.asname or alias.name: alias.name
            for node in statements
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.name in _FACTORY_NAMES
        }
        self.bindings: dict[str, list[tuple[int, _Binding]]] = {}
        self.stars: list[tuple[int, Path]] = []
        self.exports: set[str] | None = None
        for seq, node in enumerate(statements):
            for name, binding in self._bindings_of(node, seq):
                self.bindings.setdefault(name, []).append((seq, binding))
        if everywhere:
            for expr in ast.walk(tree):
                if isinstance(expr, ast.NamedExpr) and isinstance(
                    expr.target, ast.Name
                ):
                    binding = self._value(expr.target.id, expr.value)
                    self.bindings.setdefault(expr.target.id, []).append((-1, binding))

    def factory(self, func: ast.expr, depth: int = 0) -> str | None:
        """The sdk factory a callee names (through an import alias or a local
        ``make = Contract``), else its name."""
        if isinstance(func, ast.Attribute):
            return func.attr
        if not isinstance(func, ast.Name):
            return None
        if func.id in self.factories:
            return self.factories[func.id]
        if depth < _MAX_REEXPORT_DEPTH:
            for _seq, binding in self.bindings.get(func.id, []):
                if binding[0] == "alias":
                    found = self.factory(binding[1], depth + 1)
                    if found in _FACTORY_NAMES:
                        return found
        return func.id

    def _value(self, name: str, value: ast.expr | None) -> _Binding:
        if isinstance(value, ast.Call) and self.factory(value.func) == "ContractRef":
            first = value.args[0] if value.args else None
            for kw in value.keywords:
                if kw.arg == "name":
                    first = kw.value
            contract = (
                first.value
                if isinstance(first, ast.Constant) and isinstance(first.value, str)
                else None
            )
            return ("ref", contract)
        if isinstance(value, ast.Name | ast.Attribute):
            return ("alias", value)
        return ("other", name)

    def _targets(
        self, target: ast.expr, value: ast.expr | None
    ) -> Iterator[tuple[str, _Binding]]:
        if isinstance(target, ast.Name):
            yield target.id, self._value(target.id, value)
        elif isinstance(target, ast.Tuple | ast.List):
            values: list[ast.expr | None] = [None] * len(target.elts)
            if isinstance(value, ast.Tuple | ast.List) and len(value.elts) == len(
                values
            ):
                values = list(value.elts)
            for elt, item in zip(target.elts, values, strict=True):
                yield from self._targets(elt, item)
        elif isinstance(target, ast.Starred):
            yield from self._targets(target.value, None)

    def _import_bindings(
        self, node: ast.Import | ast.ImportFrom, seq: int
    ) -> Iterator[tuple[str, _Binding]]:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    module = alias.name
                    yield alias.asname, ("module", _module_file(module), module)
                else:
                    head = alias.name.split(".")[0]
                    yield head, ("package", _module_file(head), head)
            return
        source = _import_file(node, self.path)
        for alias in node.names:
            if alias.name != "*":
                yield alias.asname or alias.name, ("import", source, alias.name)
            elif source is not None:
                self.stars.append((seq, source))

    def _note_exports(self, node: ast.stmt) -> None:
        """``__all__ = [...]`` / ``__all__ += [...]`` (what a star import binds)."""
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            self.exports = _string_list(node.value)
        elif (
            isinstance(node, ast.AugAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "__all__"
            and self.exports is not None
        ):
            self.exports |= _string_list(node.value) or set()

    def _bindings_of(self, node: ast.stmt, seq: int) -> Iterator[tuple[str, _Binding]]:
        self._note_exports(node)
        if isinstance(node, ast.Import | ast.ImportFrom):
            yield from self._import_bindings(node, seq)
        elif isinstance(node, ast.ClassDef):
            yield node.name, ("class", _atom_value(node))
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node.name, ("func", node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                yield from self._targets(target, node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            yield from self._targets(node.target, node.value)


@functools.lru_cache(maxsize=1024)
def _file_classes(path: Path) -> _FileInfo | None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None
    return _FileInfo(tree, path)


def _exports(source: Path, name: str) -> bool:
    """``from source import *`` binds ``name`` (``__all__``, else public names)."""
    info = _file_classes(source)
    if info is None:
        return False
    return (
        name in info.exports if info.exports is not None else not name.startswith("_")
    )


def _submodule(path: Path, name: str) -> list[_Resolved]:
    """``name`` as a submodule of the package whose ``__init__.py`` is ``path``."""
    if path.name != "__init__.py":
        return []
    for candidate in (path.parent / f"{name}.py", path.parent / name / "__init__.py"):
        if candidate.is_file():
            return [(candidate, ("module", candidate, None))]
    return []


def _lookup(path: Path, name: str, depth: int) -> list[_Resolved]:
    info = _file_classes(path)
    return [] if info is None else _resolve_name(info, name, depth)


def _resolve_name(info: _FileInfo, name: str, depth: int = 0) -> list[_Resolved]:
    """What ``name`` is bound to in ``info``, through imports, re-exports, star
    imports and aliases: ``(defining file, binding)`` of each candidate."""
    if depth > _MAX_REEXPORT_DEPTH:
        return []
    entries = info.bindings.get(name, [])
    if info.everywhere:
        out = [r for _seq, binding in entries for r in _follow(info, binding, depth)]
        for _seq, source in info.stars:
            if _exports(source, name):
                out.extend(_lookup(source, name, depth + 1))
        return out
    last = entries[-1][0] if entries else -1
    for seq, source in reversed(info.stars):
        if seq > last and _exports(source, name):
            found = _lookup(source, name, depth + 1)
            if found:
                return found
    if entries:
        return _follow(info, entries[-1][1], depth)
    return _submodule(info.path, name) if info.path is not None else []


def _follow(info: _FileInfo, binding: _Binding, depth: int) -> list[_Resolved]:
    kind = binding[0]
    if kind == "import":
        source, name = binding[1], binding[2]
        if source is None:
            return [(None, binding)]
        return _lookup(source, name, depth + 1)
    if kind == "alias":
        return _resolve_expr(info, binding[1], depth + 1)
    return [(info.path, binding)]


def _module_attr(binding: _Binding, attr: str, depth: int) -> list[_Resolved]:
    """``attr`` of the module a ``module`` / ``package`` binding names."""
    _kind, path, dotted = binding
    if path is not None:
        found = _lookup(path, attr, depth)
        if found:
            return found
    if dotted:
        sub = _module_file(f"{dotted}.{attr}")
        if sub is not None:
            return [(sub, ("module", sub, f"{dotted}.{attr}"))]
    return []


def _resolve_expr(info: _FileInfo, expr: ast.expr, depth: int = 0) -> list[_Resolved]:
    """What a name or a dotted ``module.NAME`` expression resolves to."""
    if depth > _MAX_REEXPORT_DEPTH:
        return []
    if isinstance(expr, ast.Name):
        return _resolve_name(info, expr.id, depth)
    if isinstance(expr, ast.Attribute):
        return [
            found
            for _path, base in _resolve_expr(info, expr.value, depth)
            if base[0] in _MODULE_KINDS
            for found in _module_attr(base, expr.attr, depth + 1)
        ]
    return []


def _literal(resolved: list[_Resolved]) -> AtomLiteral | None:
    for _path, binding in resolved:
        if binding[0] == "class" and binding[1]:
            literal: AtomLiteral = binding[1]
            return literal
    return None


def _names_of(info: _FileInfo, depth: int = 0) -> set[str]:
    """Every name a file binds at module level, star imports included."""
    names = set(info.bindings)
    if depth < _MAX_REEXPORT_DEPTH:
        for _seq, source in info.stars:
            star = _file_classes(source)
            if star is not None:
                names |= {n for n in _names_of(star, depth + 1) if _exports(source, n)}
    return names


def atom_literals(tree: ast.AST, origin: Path | None = None) -> dict[str, AtomLiteral]:
    """``{local name: literal}`` of the atoms a file defines or imports.

    A name imported from a module (``from x import Atom as A``) is keyed by its
    local name; a name reached through a module alias (``import x as m`` /
    ``from pkg import x``) is keyed ``"m.Atom"``. The last binding of a name
    decides: a class defined after an import of the same name shadows it.
    """
    info = _FileInfo(tree, origin)
    found: dict[str, AtomLiteral] = {}
    for name in sorted(_names_of(info)):
        resolved = _resolve_name(info, name)
        literal = _literal(resolved)
        if literal is not None:
            found[name] = literal
            continue
        for _path, binding in resolved:
            if binding[0] == "module":  # never a bare ``import a``
                _add_module_atoms(found, name, binding)
    return found


def _add_module_atoms(
    found: dict[str, AtomLiteral], alias: str, binding: _Binding
) -> None:
    path = binding[1]
    info = _file_classes(path) if path is not None else None
    if info is None:
        return
    for name in _names_of(info):
        literal = _literal(_resolve_name(info, name))
        if literal is not None:
            found[f"{alias}.{name}"] = literal


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
    """An atom whose account is stated with ``of=``: a contract atom that
    binds an account, a claim-fact atom (``fact``), or a claim-read atom whose
    live check (``otherwise``) binds one."""
    if _kinds(literal) & {"account_type", "account_id"} or "fact" in literal:
        return True
    otherwise = literal.get("otherwise")
    return isinstance(otherwise, dict) and bool(
        _kinds(otherwise) & {"account_type", "account_id"}
    )


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
# hand-written class calling a declared access check (answer #22, C034)
# --------------------------------------------------------------------------- #
def _stub_methods(path: Path, contract: str | None) -> dict[str, AtomLiteral]:
    """``{method: literal}`` of the access checks a stub file declares for
    ``contract`` (every contract when it is not stated as a constant)."""
    info = _file_classes(path)
    if info is None:
        return {}
    out: dict[str, AtomLiteral] = {}
    for entries in info.bindings.values():
        for _seq, binding in entries:
            literal = binding[1] if binding[0] == "class" else None
            if (
                literal
                and "method" in literal
                and "contract" in literal
                and (contract is None or literal["contract"] == contract)
            ):
                out[str(literal["method"])] = literal
    return out


@functools.lru_cache(maxsize=1024)
def _declared_methods(path: Path, contract: str | None) -> dict[str, AtomLiteral]:
    """The access checks declared for the ContractRef bound in ``path``: from
    that file when it is the generated stub, else from the stub ``sandloom
    generate`` writes for the contract (``_generated/contracts`` or
    ``shared/contracts`` above ``path``) — a ContractRef written by hand
    reaches the same declarations."""
    methods = _stub_methods(path, contract)
    if methods or contract is None:
        return methods
    stem = "_".join(contract.split("."))
    for parent in list(path.parents)[:_MAX_STUB_ANCESTORS]:
        for stub in (
            parent / _GENERATED_DIR / "contracts" / f"{stem}.py",
            parent / "shared" / "contracts" / f"{stem}.py",
        ):
            if stub != path and stub.is_file():
                methods = _stub_methods(stub, contract)
                if methods:
                    return methods
    return {}


def _package_root(path: Path) -> Path:
    """The top-most package directory above ``path`` (``path``'s directory
    when it is no package)."""
    root = path.parent
    while (root.parent / "__init__.py").is_file() and root.parent != root:
        root = root.parent
    return root


_Methods = dict[str, AtomLiteral]
_CLIENTS: dict[Path, _Clients | None] = {}


def _file_clients(path: Path) -> _Clients | None:
    """The client analysis of another file of the package (once per run; a
    cycle of imports reads as no client)."""
    if path in _CLIENTS:
        return _CLIENTS[path]
    _CLIENTS[path] = None
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None
    result = _Clients(tree, path)
    _CLIENTS[path] = result
    return result


def _merge(table: dict[str, _Methods], key: str, methods: _Methods) -> bool:
    before = table.get(key, {})
    if methods.keys() <= before.keys():
        return False
    table[key] = {**before, **methods}
    return True


class _Clients:
    """The contract clients of one file and what calls a declared access
    check through one.

    ``names`` / ``attrs``: variables and attributes bound to a client;
    ``returns``: functions that return one; ``bound``: names bound to a
    declared method (``check = client.method``); ``tainted``: functions whose
    body calls a declared access check (directly or through another).
    """

    def __init__(self, tree: ast.AST, origin: Path | None) -> None:
        self.info = _FileInfo(tree, origin, everywhere=True)
        self.origin = origin
        self.root = _package_root(origin) if origin is not None else None
        self.names: dict[str, _Methods] = {}
        self.attrs: dict[str, _Methods] = {}
        self.returns: dict[str, _Methods] = {}
        self.bound: dict[str, AtomLiteral] = {}
        self.tainted: dict[str, AtomLiteral] = {}
        self._refs: dict[int, _Methods] = {}
        self._foreign: dict[tuple[str, str], Any] = {}
        self._solve(tree)

    # -- resolution ---------------------------------------------------------
    def ref_methods(self, expr: ast.expr) -> _Methods:
        """The declared checks of the ContractRef ``expr`` resolves to."""
        key = id(expr)
        if key not in self._refs:
            out: _Methods = {}
            for path, binding in _resolve_expr(self.info, expr):
                if binding[0] == "ref" and path is not None:
                    out.update(_declared_methods(path, binding[1]))
            self._refs[key] = out
        return self._refs[key]

    def _near(self, path: Path | None) -> bool:
        if path is None or self.root is None or path == self.origin:
            return False
        return path.is_relative_to(self.root)

    def _near_resolve(self, expr: ast.expr, depth: int = 0) -> list[_Resolved]:
        """``expr`` resolved through imports of files of the same package only."""
        if depth > _MAX_REEXPORT_DEPTH:
            return []
        if isinstance(expr, ast.Name):
            return [
                found
                for _seq, binding in self.info.bindings.get(expr.id, [])
                if binding[0] in ("import", *_MODULE_KINDS) and self._near(binding[1])
                for found in (
                    _follow(self.info, binding, depth)
                    if binding[0] == "import"
                    else [(binding[1], binding)]
                )
            ]
        if isinstance(expr, ast.Attribute):
            return [
                found
                for path, base in self._near_resolve(expr.value, depth + 1)
                if base[0] in _MODULE_KINDS and self._near(path)
                for found in _module_attr(base, expr.attr, depth + 1)
            ]
        return []

    def _imported(self, expr: ast.expr, table: str) -> Any:
        """``table`` of the file of the package that defines ``expr``."""
        key = (ast.dump(expr), table)
        if key in self._foreign:
            return self._foreign[key]
        self._foreign[key] = {}
        out: dict[str, Any] = {}
        for path, binding in self._near_resolve(expr):
            if binding[0] not in ("other", "func") or not self._near(path):
                continue
            clients = _file_clients(path) if path is not None else None
            value = getattr(clients, table, {}).get(binding[1]) if clients else None
            if value:
                out = value if table in ("bound", "tainted") else {**out, **value}
        self._foreign[key] = out
        return out

    # -- clients ------------------------------------------------------------
    def client(self, expr: ast.expr | None) -> _Methods:
        """``{method: literal}`` when ``expr`` evaluates to a contract client."""
        if expr is None:
            return {}
        if isinstance(expr, ast.Await | ast.NamedExpr):
            return self.client(expr.value)
        if isinstance(expr, ast.IfExp):
            return {**self.client(expr.body), **self.client(expr.orelse)}
        if isinstance(expr, ast.BoolOp):
            return {k: v for item in expr.values for k, v in self.client(item).items()}
        if isinstance(expr, ast.Call):
            return self._call_client(expr)
        if isinstance(expr, ast.Name):
            return self.names.get(expr.id) or self._imported(expr, "names")
        if isinstance(expr, ast.Attribute):
            return self.attrs.get(expr.attr) or self._imported(expr, "names")
        return {}

    def _call_client(self, call: ast.Call) -> _Methods:
        """A call that returns a client: ``Contract(ref)``, its ``.client()``,
        or a function (of this file or the package) that returns one."""
        if self.info.factory(call.func) in _CLIENT_FACTORIES:
            ref = call.args[0] if call.args else None
            for kw in call.keywords:
                if kw.arg == "ref":
                    ref = kw.value
            return self.ref_methods(ref) if ref is not None else {}
        if isinstance(call.func, ast.Attribute) and call.func.attr == "client":
            found = self.client(call.func.value)  # OptionalContract.client()
            if found:
                return found
        name = _callee(call)
        local = self.returns.get(name, {}) if name else {}
        return local or self._imported(call.func, "returns")

    def method_ref(self, expr: ast.expr) -> AtomLiteral | None:
        """The declared check ``expr`` names: ``client.method``, a constant
        ``getattr(client, "method")`` or a name bound to one of those."""
        if isinstance(expr, ast.Attribute):
            found = self.client(expr.value).get(expr.attr)
            if found:
                return found
        if (
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Name)
            and expr.func.id == "getattr"
            and len(expr.args) >= 2
            and isinstance(expr.args[1], ast.Constant)
            and isinstance(expr.args[1].value, str)
        ):
            return self.client(expr.args[0]).get(expr.args[1].value)
        if isinstance(expr, ast.Name):
            return self.bound.get(expr.id) or self._imported(expr, "bound") or None
        return None

    def first_check(self, node: ast.AST) -> AtomLiteral | None:
        """The first declared check a node's calls reach, through helpers."""
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            found = self.method_ref(call.func)
            if found:
                return found
            # a helper of this file is called by its name; a method is not
            # followed (a class calling the check itself is reported)
            if isinstance(call.func, ast.Name) and call.func.id in self.tainted:
                return self.tainted[call.func.id]
            if isinstance(call.func, ast.Name | ast.Attribute):
                imported = self._imported(call.func, "tainted")
                if imported:
                    return dict(imported)
        return None

    def _bind(self, target: ast.expr, methods: _Methods) -> bool:
        if isinstance(target, ast.Name):
            # a class-body name is read back as ``self.name``
            return _merge(self.names, target.id, methods) | _merge(
                self.attrs, target.id, methods
            )
        if isinstance(target, ast.Attribute):
            return _merge(self.attrs, target.attr, methods)
        return False

    def _solve(self, tree: ast.AST) -> None:
        """Bind clients, bound methods, client-returning and checking functions
        until nothing changes (each round only adds, so it ends)."""
        pairs, functions = _assignments(tree)
        returns = {
            id(fn): [
                n.value
                for n in ast.walk(fn)
                if isinstance(n, ast.Return) and n.value is not None
            ]
            for fn in functions
        }
        while self._bind_pairs(pairs) | self._bind_functions(functions, returns):
            pass

    def _bind_pairs(self, pairs: list[tuple[ast.expr, ast.expr]]) -> bool:
        changed = False
        for target, value in pairs:
            methods = self.client(value)
            if methods:
                changed |= self._bind(target, methods)
            bound = self.method_ref(value)
            if bound and isinstance(target, ast.Name) and target.id not in self.bound:
                self.bound[target.id] = bound
                changed = True
        return changed

    def _bind_functions(
        self,
        functions: list[ast.FunctionDef | ast.AsyncFunctionDef],
        returns: dict[int, list[ast.expr]],
    ) -> bool:
        changed = False
        for fn in functions:
            for value in returns[id(fn)]:
                methods = self.client(value)
                if methods:
                    changed |= _merge(self.returns, fn.name, methods)
            if fn.name not in self.tainted:
                found = self.first_check(fn)
                if found:
                    self.tainted[fn.name] = found
                    changed = True
        return changed


def _assignments(
    tree: ast.AST,
) -> tuple[
    list[tuple[ast.expr, ast.expr]], list[ast.FunctionDef | ast.AsyncFunctionDef]
]:
    """Every ``(target, value)`` a tree binds (assignments, annotations,
    walrus, ``with … as``, parameter defaults) and every function."""
    pairs: list[tuple[ast.expr, ast.expr]] = []
    functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                pairs.extend(_pairs(target, node.value))
        elif isinstance(node, ast.AnnAssign | ast.NamedExpr) and node.value is not None:
            pairs.extend(_pairs(node.target, node.value))
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            pairs.extend(_pairs(node.optional_vars, node.context_expr))
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            functions.append(node)
            pairs.extend(_default_pairs(node.args))
    return pairs, functions


def _default_pairs(args: ast.arguments) -> list[tuple[ast.expr, ast.expr]]:
    positional = [*args.posonlyargs, *args.args]
    with_default = positional[len(positional) - len(args.defaults) :]
    pairs: list[tuple[ast.expr, ast.expr]] = [
        (ast.Name(id=arg.arg), default)
        for arg, default in zip(with_default, args.defaults, strict=True)
    ]
    pairs.extend(
        (ast.Name(id=arg.arg), default)
        for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True)
        if default is not None
    )
    return pairs


def _pairs(target: ast.expr, value: ast.expr) -> list[tuple[ast.expr, ast.expr]]:
    """``(target, value)`` of an assignment, element-wise for tuples."""
    if (
        isinstance(target, ast.Tuple | ast.List)
        and isinstance(value, ast.Tuple | ast.List)
        and len(target.elts) == len(value.elts)
    ):
        return [
            p
            for t, v in zip(target.elts, value.elts, strict=True)
            for p in _pairs(t, v)
        ]
    return [(target, value)]


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


def _is_generated_atom(cls: ast.ClassDef, origin: Path | None) -> bool:
    """A class ``sandloom generate`` wrote: its literal, in a ``_generated/``
    package. The literal alone exempts nothing (anyone can type it)."""
    return (
        origin is not None
        and _GENERATED_DIR in origin.parts
        and _atom_value(cls) is not None
    )


def handwritten_access_checks(tree: ast.AST, origin: Path | None = None) -> list[str]:
    """``Line N: ...`` for each hand-written class whose body calls a declared
    access check with other metadata than its declaration: another tier or
    category, or a declared risk the class does not state (``risks()``)."""
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    if not classes:
        return []
    clients = _Clients(tree, origin)
    out: list[str] = []
    for cls in classes:
        if _is_generated_atom(cls, origin):
            continue
        literal = clients.first_check(cls)
        if literal is None:
            continue
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
