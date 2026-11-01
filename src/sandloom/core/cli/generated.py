"""The generated files: ``sandloom generate`` writes them, ``sandloom test``
checks them (Public API 1.0, spec §5.2, §5.3, §5.7, §5.8).

For every module declared with L1 discovery (``define_module`` of
``sandloom.sdk``):

- ``<module>/module.snapshot.json``: the module's declared surface, sections
  ``events`` (producer schemas), ``consumers``, ``consumes``, ``contracts``,
  ``sagas`` and ``projections`` (``routes`` stay in ``routes.snapshot.json``
  and contract describes in ``contracts.snapshot.json`` until model E's
  route snapshot moves them here);
- ``<shared>/events/<channel>.json``: each producer's schema, so views in
  other repositories can be verified (SANDLOOM-C025);
- ``<shared>/contracts/<x_y_vN>.py``: the typed stub of each ``@contract``
  provider (the Protocol and ``X_Y_VN: ContractRef[Protocol]``);
- ``<shared>/contracts/<module>_dto.py``: the wire-shape copies of the
  provider's own DTOs its contract signatures name
  (:mod:`sandloom.core.cli.dto_copies`, SANDLOOM-C012);
- ``<module>/routes.snapshot.json``: the module's routes and their policy
  (a route that lost its authentication is incompatible, SANDLOOM-T001);
- ``<module>/contracts.snapshot.json``: the describe documents of the
  contracts it provides (a breaking change is incompatible, SANDLOOM-T004);
- ``<shared>/admin-manifest.json``: the admin console's screens, when the
  composition loads the console (SANDLOOM-T016).

Every generated file is compared with the committed version **before
anything is written**: an incompatible change fails and nothing is written
(SANDLOOM-T005 for an event schema, SANDLOOM-T007 for a contract stub,
SANDLOOM-T004 for a contract snapshot, SANDLOOM-C012 for a DTO the generator
cannot copy, SANDLOOM-T001 for a weaker route policy, approved only by
``sandloom routes snapshot --write``). ``sandloom generate`` writes the
compatible changes; ``sandloom test`` (``--check``) never writes and fails on a
stale file: "generated files are stale: run `sandloom generate`".
"""

from __future__ import annotations

import argparse
import ast
import datetime as _dt
import decimal
import importlib
import inspect
import json
import sys
import types
import typing
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sandloom.core.cli.dto_copies as _dto_copies
import sandloom.core.cli.inspection.generation as _inspection_generation
import sandloom.core.cli.route_weakening as _route_weakening
import sandloom.core.contract_compat as _contract_compat
import sandloom.core.route_snapshots as _route_snapshots
from sandloom.core.admin import compile_app
from sandloom.core.archcheck.manifest import load_rules
from sandloom.core.composition import load_composition_app
from sandloom.core.contracts.schema_compatibility import (
    schema_problems as schema_problems,
)
from sandloom.core.descriptors.module_names import module_name
from sandloom.core.module_api import contract_schema

#: File name of a module's generated surface snapshot.
MODULE_SNAPSHOT = "module.snapshot.json"
ADMIN_MANIFEST = "admin-manifest.json"
HEADER = _dto_copies.HEADER


@dataclass
class Generated:
    """One generated file: its target, content and verdict against the
    committed version."""

    path: Path
    content: str
    code: str
    problems: list[str] = field(default_factory=list)

    @property
    def current(self) -> str | None:
        return self.path.read_text(encoding="utf-8") if self.path.is_file() else None

    @property
    def stale(self) -> bool:
        return self.current != self.content


# --------------------------------------------------------------------------- #
# Contract stubs
# --------------------------------------------------------------------------- #


def stub_names(contract: str) -> tuple[str, str, str]:
    """``(module file stem, Protocol name, reference constant)`` of a contract."""
    parts = contract.split(".")
    stem = "_".join(parts)
    protocol = "".join(p[:1].upper() + p[1:] for p in "_".join(parts).split("_"))
    return stem, protocol, stem.upper()


_STDLIB = frozenset(
    {"typing", "uuid", "datetime", "decimal", "collections.abc", "enum", "__future__"}
)
_LINE = 88


class _Imports:
    def __init__(self) -> None:
        self.names: dict[str, set[str]] = {}

    def need(self, module: str, name: str) -> str:
        self.names.setdefault(module, set()).add(name)
        return name

    def render(self) -> str:
        """isort order: the standard library, then everything else."""
        groups: list[list[str]] = [[], []]
        for module in sorted(self.names):
            names = ", ".join(sorted(self.names[module]))
            line = f"from {module} import {names}"
            if len(line) > _LINE:
                inner = "".join(f"    {n},\n" for n in sorted(self.names[module]))
                line = f"from {module} import (\n{inner})"
            groups[0 if module in _STDLIB else 1].append(line)
        return "\n\n".join("\n".join(g) for g in groups if g)


def docstring(doc: str | None, indent: str) -> str | None:
    """``doc`` as a docstring at ``indent``, in ``ruff format``'s layout
    (``None`` without a docstring). The provider's own docstring, so
    ``sandloom docs <contract>`` and an editor show the semantics to callers."""
    if not doc or not doc.strip():
        return None
    text = doc.strip().replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    lines = text.splitlines()
    if len(lines) == 1:
        return f'{indent}"""{lines[0]}"""'
    body = "\n".join(f"{indent}{line}".rstrip() for line in lines[1:])
    return f'{indent}"""{lines[0]}\n{body}\n{indent}"""'


def _method(name: str, params: list[str], returns: str, doc: str | None = None) -> str:
    """One Protocol method, wrapped the way ``ruff format`` does; its body is
    the provider method's docstring when it has one (else ``...``)."""
    text = docstring(doc, "        ")
    end = ":\n" + text if text is not None else ": ..."
    flat = f"    async def {name}({', '.join(params)}) -> {returns}"
    # The signature line ends with ``:`` before a docstring body and with
    # ``: ...`` without one: only that tail counts against the line length.
    tail = ":" if text is not None else ": ..."
    if len(flat) + len(tail) <= _LINE:
        return flat + end
    middle = f"        {', '.join(params)}"
    if len(middle) <= _LINE:
        return f"    async def {name}(\n{middle}\n    ) -> {returns}{end}"
    lines = "".join(f"        {p},\n" for p in params)
    return f"    async def {name}(\n{lines}    ) -> {returns}{end}"


_SIMPLE = {
    int: "int",
    str: "str",
    float: "float",
    bool: "bool",
    bytes: "bytes",
    type(None): "None",
}
_FROM = {
    uuid.UUID: ("uuid", "UUID"),
    _dt.datetime: ("datetime", "datetime"),
    _dt.date: ("datetime", "date"),
    _dt.time: ("datetime", "time"),
    _dt.timedelta: ("datetime", "timedelta"),
    decimal.Decimal: ("decimal", "Decimal"),
}
_GENERIC = {
    list: "list",
    dict: "dict",
    tuple: "tuple",
    set: "set",
    frozenset: "frozenset",
}


def _literal(value: Any) -> str:
    """Python source of a scalar default / ``Literal`` value in ruff's style
    (double-quoted strings), so a generated file passes ``ruff format``."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return repr(value)


def _render_generic(
    tp: Any, imports: _Imports, resolver: _dto_copies.Resolver | None
) -> str | None:
    """``X | Y``, ``Literal[...]``, ``list[...]``, ``collections.abc`` forms."""
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin is typing.Annotated:
        return render_annotation(args[0], imports, resolver)
    if origin in (typing.Union, types.UnionType):
        return " | ".join(render_annotation(a, imports, resolver) for a in args)
    if origin is typing.Literal:
        imports.need("typing", "Literal")
        return f"Literal[{', '.join(_literal(a) for a in args)}]"
    if origin in _GENERIC:
        inner = ", ".join(
            "..." if a is Ellipsis else render_annotation(a, imports, resolver)
            for a in args
        )
        return f"{_GENERIC[origin]}[{inner}]" if args else _GENERIC[origin]
    if origin is not None and getattr(origin, "__module__", "") == "collections.abc":
        name = imports.need("collections.abc", origin.__name__)
        inner = ", ".join(render_annotation(a, imports, resolver) for a in args)
        return f"{name}[{inner}]" if args else name
    return None


def render_annotation(
    tp: Any, imports: _Imports, resolver: _dto_copies.Resolver | None = None
) -> str:
    """Python source of a wire annotation, collecting the imports it needs.

    A DTO (pydantic model, Enum) is imported from where ``resolver`` places
    it: its generated copy for a class of the provider's own package, else
    its shared package (without a resolver: its own module)."""
    if tp in _SIMPLE:
        return _SIMPLE[tp]
    if tp in _FROM:
        module, name = _FROM[tp]
        return imports.need(module, name)
    if tp is typing.Any:
        return imports.need("typing", "Any")
    generic = _render_generic(tp, imports, resolver)
    if generic is not None:
        return generic
    if isinstance(tp, type):  # a DTO (pydantic model, Enum)
        if resolver is not None:
            module, name = resolver.place(tp, tp.__qualname__)
            return imports.need(module, name)
        return imports.need(tp.__module__, tp.__name__)
    return imports.need("typing", "Any")


def _declaration(ref: str, protocol: str, literal: str) -> str:
    """``REF: ContractRef[Protocol] = ContractRef("<contract>", Protocol)``
    wrapped the way ``ruff format`` wraps an annotated assignment: the
    value's call first (its arguments on one line, else one per line); when
    the head still overflows, the annotation's subscript too."""
    args = f"{literal}, {protocol}"
    call = (
        f"ContractRef(\n    {args}\n)"
        if len(f"    {args}") <= _LINE
        else f"ContractRef(\n    {literal},\n    {protocol},\n)"
    )
    head = f"{ref}: ContractRef[{protocol}] = "
    if len(f"{head}ContractRef({args})") <= _LINE:
        return f"{head}ContractRef({args})"
    if len(f"{head}ContractRef(") <= _LINE:
        return f"{head}{call}"
    annotation = f"{ref}: ContractRef[\n    {protocol}\n]"
    if len(f"] = ContractRef({args})") <= _LINE:
        return f"{annotation} = ContractRef({args})"
    return f"{annotation} = {call}"


def render_stub(
    contract: str, provider: Any, resolver: _dto_copies.Resolver | None = None
) -> str:
    """The generated stub of contract ``contract`` provided by ``provider``
    (its DTOs imported from where ``resolver`` places them)."""
    _stem, protocol, ref = stub_names(contract)
    imports = _Imports()
    imports.need("typing", "Protocol")
    imports.need("sandloom.core.descriptors.contract_refs", "ContractRef")
    methods = []
    documented = False
    for name, runtime in sorted(contract_schema(provider).methods.items()):
        params = ["self"]
        for pname, param in runtime.signature.parameters.items():
            annotation = runtime.annotations[pname]
            text = f"{pname}: {render_annotation(annotation, imports, resolver)}"
            if param.kind is param.KEYWORD_ONLY and "*" not in params:
                params.append("*")
            if param.default is not param.empty:
                text += f" = {_literal(param.default)}"
            params.append(text)
        method = getattr(provider, name, None)
        doc = inspect.getdoc(method) if method is not None else None
        documented = documented or bool(doc and doc.strip())
        methods.append(
            _method(
                name, params, render_annotation(runtime.returns, imports, resolver), doc
            )
        )
    own = provider.__dict__.get("__doc__")
    class_doc = docstring(inspect.cleandoc(own) if own else None, "    ")
    # A documented method is a multi-line block: ruff format separates them.
    separator = "\n\n" if documented else "\n"
    body = separator.join(methods)
    if class_doc is not None:
        body = f"{class_doc}\n\n{body}" if methods else class_doc
    body = body or "    pass"
    declaration = _declaration(ref, protocol, json.dumps(contract))
    return (
        f'{HEADER}"""Contract ``{contract}``: the typed stub callers import.\n\n'
        f"Generated from ``{provider.__module__}.{provider.__qualname__}``.\n"
        f'"""\n\n'
        "from __future__ import annotations\n\n"
        f"{imports.render()}\n\n\n"
        f"class {protocol}(Protocol):\n{body}\n\n\n"
        f"{declaration}\n"
    )


def _stub_methods(source: str) -> dict[str, str]:
    """``{method: signature text}`` of the Protocol in a committed stub."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.AsyncFunctionDef):
                    args = ast.unparse(item.args)
                    returns = ast.unparse(item.returns) if item.returns else ""
                    out[item.name] = f"({args}) -> {returns}"
    return out


def stub_problems(old: str | None, new: str) -> list[str]:
    """Incompatible differences of a regenerated stub (a removed or changed
    method); an added method is compatible."""
    if old is None:
        return []
    before, after = _stub_methods(old), _stub_methods(new)
    problems = []
    for name, signature in before.items():
        if name not in after:
            problems.append(f"method {name!r} was removed")
        elif after[name] != signature:
            problems.append(f"method {name!r} changed: {signature} -> {after[name]}")
    return problems


# --------------------------------------------------------------------------- #
# Event schemas
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


def route_problems(old: str | None, new: Mapping[str, Any]) -> list[str]:
    """A route policy that became weaker (spec §5.8, G-ROUTE-04): see
    :mod:`sandloom.core.cli.route_weakening` for every kind. ``sandloom test`` never
    writes one; ``sandloom routes snapshot --write`` is the explicit approval."""
    return _route_weakening.weakenings(old, new)


def _dump(doc: Any) -> str:
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _module_dir(module: str) -> Path | None:
    imported = importlib.import_module(module)
    file = getattr(imported, "__file__", None)
    return Path(file).parent if file else None


def is_l1(module: str) -> bool:
    """``module`` declares itself with L1 discovery (``sandloom.sdk.define_module``)."""
    register = getattr(importlib.import_module(module), "register", None)
    return bool(getattr(register, "sandloom_l1", False))


def module_surface(entry: Any) -> dict[str, Any]:
    """The ``module.snapshot.json`` document of one module manifest entry."""
    return {
        "format": 1,
        "module": entry.name,
        "events": {
            e.channel: dict(e.schema or {})
            for e in entry.events
            if e.role == "producer"
        },
        "consumers": [
            {
                "name": c.name,
                "channel": c.channel,
                "durable": c.durable,
                "required": c.required,
                "dead_letter": c.dead_letter,
                "database": c.database,
                "group": c.group,
                "start_from": c.start_from,
                "bind_tenant_from": c.bind_tenant_from,
            }
            for c in entry.consumers
        ],
        "consumes": [
            {"name": u.name, "required": u.required, "methods": list(u.methods)}
            for u in entry.contracts_consumed
        ],
        "contracts": [
            {"name": c.name, "database": c.database, "methods": list(c.methods)}
            for c in entry.contracts_provided
        ],
        "sagas": [{"name": s.name, "version": s.version} for s in entry.sagas],
        "projections": [
            {"name": p.name, "source_channels": list(p.source_channels)}
            for p in entry.projections
        ],
    }


def _contract_snapshot(module: str, contracts: Mapping[str, Any]) -> Generated | None:
    """``contracts.snapshot.json`` of an L1 module; a breaking change of a
    provided contract (``contract_compat``) is incompatible (SANDLOOM-T004)."""
    doc = contracts.get(module)
    path = _contract_compat.snapshot_path(module)
    if doc is None or path is None:
        return None
    item = Generated(path, _contract_compat.render_snapshot(doc), "SANDLOOM-T004")
    old = json.loads(item.current).get("contracts", {}) if item.current else {}
    for name, expected in old.items():
        report = _contract_compat.compare_contract(
            name, expected, doc["contracts"].get(name)
        )
        item.problems += report.errors
    return item


def _module_snapshots(
    app: Any, module: str, contracts: Mapping[str, Any]
) -> list[Generated]:
    """The route and contract snapshots of a module this project owns."""
    out: list[Generated] = []
    routes_path = _route_snapshots.snapshot_path(module)
    if routes_path is not None:
        routes_doc = _route_snapshots.module_route_snapshots(app, [module])[module]
        routes = Generated(
            routes_path,
            _route_snapshots.render_snapshot(routes_doc),
            "SANDLOOM-T001",
        )
        routes.problems = route_problems(routes.current, routes_doc)
        out.append(routes)
    snapshot_of_contracts = _contract_snapshot(module, contracts)
    if snapshot_of_contracts is not None:
        out.append(snapshot_of_contracts)
    return out


def _owned(directory: Path, source_root: Path | None) -> bool:
    """The module's files belong to this project (not an installed package)."""
    if source_root is None:
        return True
    try:
        directory.resolve().relative_to(source_root.resolve())
    except ValueError:
        return False
    return True


def _shared_packages(facade: Any, shared_package: str | None) -> tuple[str, ...]:
    """Packages whose DTOs a stub references as they are (never copied)."""
    settings = getattr(facade, "settings", None)
    extra = tuple(getattr(settings, "contract_dto_packages", ()) or ())
    own = (shared_package,) if shared_package else ()
    return ("sandloom.shared", *own, *extra)


def _contract_files(
    entry: Any,
    facade: Any,
    shared_dir: Path,
    shared_package: str,
    shared_packages: tuple[str, ...],
    format_dto: Callable[[str, Path], str],
) -> list[Generated]:
    """The stubs of the contracts ``entry`` provides and, when their
    signatures name the provider's own DTOs, the module's generated DTO
    copies (one ``<module>_dto.py``, so two contracts share one class)."""
    short = module_name(entry.name)
    stem_dto = _dto_copies.dto_module_stem(short)
    dto_path = shared_dir / "contracts" / f"{stem_dto}.py"
    resolver = _dto_copies.Resolver(
        provider_package=entry.name,
        dto_package=f"{shared_package}.contracts.{stem_dto}",
        shared_packages=shared_packages,
    )
    out: list[Generated] = []
    refused: list[str] = []
    pending_stubs: list[tuple[Generated, str, Any]] = []
    for provided in entry.contracts_provided:
        impl = facade.service_registry.try_get(provided.name)
        impl = getattr(impl, "contract_impl", impl)
        if impl is None or not hasattr(impl, "__sandloom_contract__"):
            continue
        stem, _protocol, _ref = stub_names(provided.name)
        path = shared_dir / "contracts" / f"{stem}.py"
        try:
            content = render_stub(provided.name, impl, resolver)
        except _dto_copies.CopyError as exc:
            refused.append(f"{provided.name}: {exc}")
            continue
        stub = Generated(path, content, "SANDLOOM-T007")
        stub.problems = stub_problems(stub.current, stub.content)
        out.append(stub)
        pending_stubs.append((stub, provided.name, impl))
    if resolver.copied or refused:
        try:
            text = format_dto(_dto_copies.render_module(short, resolver), dto_path)
        except _dto_copies.CopyError as exc:
            refused.append(str(exc))
            text = ""
        copies = Generated(dto_path, text, _dto_copies.CODE)
        copies.problems = refused
        out.append(copies)
        # All contracts and nested/default DTOs are now known, so every stub
        # uses the same final names as the shared DTO module.
        if not refused:
            for stub, name, impl in pending_stubs:
                stub.content = render_stub(name, impl, resolver)
                stub.problems = stub_problems(stub.current, stub.content)
    return out


def _admin_manifest(app: Any, shared_dir: Path) -> list[Generated]:
    """The console reads this file, so every process serves the same screens
    whichever modules it hosts. Only a process that composes them all can write it."""
    names = {entry.name for entry in app.state.app_manifest.modules}
    if "sandloom_admin" not in names:
        return []
    item = Generated(shared_dir / ADMIN_MANIFEST, "", "SANDLOOM-T016")
    try:
        item.content = (
            json.dumps(compile_app(app), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        )
    except (ValueError, KeyError, AttributeError, ImportError) as exc:
        item.problems.append(f"admin declarations: {exc}")
    return [item]


def _selected(entry: Any, modules: Iterable[str] | None) -> bool:
    if modules is None:
        return True
    wanted = set(modules)
    return entry.name in wanted or module_name(entry.name) in wanted


def plan(
    app: Any,
    shared_dir: Path,
    source_root: Path | None = None,
    *,
    shared_package: str = "sandloom.shared",
    modules: Iterable[str] | None = None,
    format_dto: Callable[[str, Path], str] | None = None,
) -> list[Generated]:
    """Every generated file of the app's L1 modules, with its verdict.

    The consumer side (contract stubs, DTO copies, exported event schemas)
    is generated for every composed module into ``shared_dir`` (the package
    ``shared_package``); the module side (``module.snapshot.json``, route
    and contract snapshots) only for modules under ``source_root``: an
    installed module package ships its own. ``modules`` (names or import
    paths) keeps those modules' files only. ``format_dto`` formats a DTO
    module (default: the project's ruff)."""
    manifest = app.state.app_manifest
    facade = app.state.core
    contracts = _contract_compat.module_snapshots(facade)
    shared_packages = _shared_packages(facade, shared_package)
    if format_dto is None:
        cwd = source_root

        def format_dto(text: str, path: Path) -> str:
            return _dto_copies.ruff_formatted(text, path, cwd=cwd)

    out: list[Generated] = []
    for entry in manifest.modules:
        if not is_l1(entry.name) or not _selected(entry, modules):
            continue
        directory = _module_dir(entry.name)
        if directory is None:
            continue
        owned = _owned(directory, source_root)
        doc = module_surface(entry)
        snapshot = Generated(directory / MODULE_SNAPSHOT, _dump(doc), "SANDLOOM-T005")
        old_doc = json.loads(snapshot.current) if snapshot.current else {}
        for channel, schema in doc["events"].items():
            previous = (old_doc.get("events") or {}).get(channel)
            snapshot.problems += [
                f"{channel}: {p}" for p in schema_problems(previous, schema)
            ]
            exported = Generated(
                shared_dir / "events" / f"{channel}.json",
                _dump(schema),
                "SANDLOOM-T005",
            )
            if not owned:
                exported.problems += [
                    f"{channel}: {p}"
                    for p in schema_problems(
                        json.loads(exported.current) if exported.current else None,
                        schema,
                    )
                ]
            out.append(exported)
        if owned:
            out.append(snapshot)
            out += _module_snapshots(app, entry.name, contracts)
        out += _contract_files(
            entry, facade, shared_dir, shared_package, shared_packages, format_dto
        )
    if modules is None:
        out += _admin_manifest(app, shared_dir)
    return out


def _ensure_package(directory: Path, shared_dir: Path) -> None:
    """``__init__.py`` in the shared package and its ``contracts`` package."""
    for package in (shared_dir, directory):
        init = package / "__init__.py"
        if package.is_dir() and not init.exists():
            init.write_text(
                '"""Contract DTO package (generated stubs and DTO copies)."""\n',
                encoding="utf-8",
            )


#: The fix printed per incompatible-change code.
_FIXES = {
    "SANDLOOM-T016": "fix the admin.py declaration named above",
    "SANDLOOM-T001": "a weaker route policy is written only once approved: review "
    "it, then run `sandloom routes snapshot --write` (the snapshot diff records the "
    "approval)",
    "SANDLOOM-T004": "a breaking contract change is a new version (.v2)",
    "SANDLOOM-T005": "a breaking change is a new version (.v2)",
    "SANDLOOM-T007": "a breaking contract change is a new version (.v2)",
    "SANDLOOM-C012": "keep every contract DTO in the closed set of wire shapes, "
    "defined at module level in the provider's own package",
}
#: What a stale generated file asks for.
STALE = "generated files are stale: run `sandloom generate`"


def _shown(path: Path, root: Path | None) -> str:
    if root is not None:
        try:
            return str(path.resolve().relative_to(root.resolve()))
        except ValueError:
            pass
    return str(path)


def _report_broken(broken: list[Generated], root: Path | None) -> None:
    for item in broken:
        for problem in item.problems:
            print(
                f"{_shown(item.path, root)}: error[{item.code}]: incompatible "
                f"change: {problem}"
            )
    for code in sorted({f.code for f in broken}):
        print(
            f"fix: {_FIXES.get(code, _FIXES['SANDLOOM-T005'])}; sandloom explain {code}"
        )


def apply(
    files: Iterable[Generated],
    *,
    write: bool,
    shared_dir: Path,
    root: Path | None = None,
) -> int:
    """Fail on an incompatible change, writing nothing; then write the stale
    files (``write``: ``sandloom generate``) or fail listing them
    (``sandloom test``)."""
    files = list(files)
    broken = [f for f in files if f.problems]
    if broken:
        _report_broken(broken, root)
        if write:
            print("sandloom generate: nothing was written")
        return 1
    stale = [f for f in files if f.stale]
    if not write:
        if stale:
            print(f"{STALE} ({len(stale)} file(s)):")
        for item in stale:
            state = "missing" if item.current is None else "differs"
            print(f"  {_shown(item.path, root)} [{item.code}] ({state})")
        return 1 if stale else 0
    for item in stale:
        item.path.parent.mkdir(parents=True, exist_ok=True)
        if item.path.suffix == ".py":
            _ensure_package(item.path.parent, shared_dir)
        if item.path.name == "inspection.json" and item.path.parent.name == ".sandloom":
            _inspection_generation.publish_index(item.path, item.content)
        else:
            item.path.write_text(item.content, encoding="utf-8")
        print(f"wrote {_shown(item.path, root)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """``sandloom generate [module ...]`` (writes) and the ``generated files``
    step of ``sandloom test`` (``--check``: never writes)."""
    parser = argparse.ArgumentParser(
        prog="sandloom generate",
        description="Write every generated file (module, route and contract "
        "snapshots, contract stubs, DTO copies, exported event schemas) after "
        "the compatibility checks; --check only compares (sandloom test)",
    )
    parser.add_argument(
        "modules", nargs="*", help="only these modules' files (default: every module)"
    )
    parser.add_argument("--check", action="store_true", help="never write")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    rules = load_rules(args.root / "architecture_rules.yml")
    if str(rules.source_root) not in sys.path:
        sys.path.insert(0, str(rules.source_root))
    before = _inspection_generation.capture(rules)
    app = load_composition_app()
    if args.modules:
        known = {
            name
            for entry in app.state.app_manifest.modules
            for name in (entry.name, module_name(entry.name))
        }
        unknown = sorted(set(args.modules) - known)
        if unknown:
            print(f"sandloom generate: no composed module named {', '.join(unknown)}")
            return 2
    files = plan(
        app,
        rules.shared_dir,
        rules.source_root,
        shared_package=rules.shared_dto_package,
        modules=args.modules or None,
    )
    return _inspection_generation.finish(
        rules=rules,
        app=app,
        selected=files,
        modules=args.modules,
        check=args.check,
        before=before,
        plan_all=lambda: plan(
            app,
            rules.shared_dir,
            rules.source_root,
            shared_package=rules.shared_dto_package,
        ),
        make_file=Generated,
        apply_files=apply,
    )


__all__ = [
    "HEADER",
    "MODULE_SNAPSHOT",
    "STALE",
    "Generated",
    "apply",
    "is_l1",
    "main",
    "module_surface",
    "plan",
    "render_annotation",
    "render_stub",
    "schema_problems",
    "stub_names",
    "stub_problems",
]
