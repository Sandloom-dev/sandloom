"""The generated files: ``sandloom generate`` writes them, ``sandloom test``
checks them (Public API 1.0, spec §5.2, §5.3, §5.7, §5.8).

For every module declared with L1 discovery (``define_module`` of
``sandloom.sdk``):

- ``<module>/module.snapshot.json``: the module's declared surface, sections
  ``events`` (producer schemas), ``consumers``, ``consumes``,
  ``capabilities`` (declared or derived), ``contracts``, ``sagas`` and
  ``projections`` (``routes`` stay in ``routes.snapshot.json``
  and contract describes in ``contracts.snapshot.json`` until model E's
  route snapshot moves them here);
- in an application only, ``<shared>/events/<channel>.json``: each producer's
  schema, so views can be verified (SANDLOOM-C025); in a tree of module
  distributions a view is checked against the producer's ``module.snapshot.json``;
- in an application only, ``<shared>/contracts/<x_y_vN>.py``: the typed stub of
  each ``@contract`` provider (the Protocol and ``X_Y_VN: ContractRef[Protocol]``);
- in an application only, ``<shared>/contracts/<module>_dto.py``: the
  wire-shape copies of the provider's own DTOs its contract signatures name
  (:mod:`sandloom.core.cli.dto_copies`, SANDLOOM-C012);
- ``<module>/routes.snapshot.json``: the module's routes and their policy
  (a route that lost its authentication is incompatible, SANDLOOM-T001);
- ``<module>/contracts.snapshot.json``: the describe documents of the
  contracts it provides (a breaking change is incompatible, SANDLOOM-T004);
- in a tree of module distributions (top-level module packages),
  ``<module>/_generated/contracts/``: the stubs, DTO copies and narrowed
  expectations of the contracts the module ``consumes``, planned before
  composition (:mod:`sandloom.core.cli.consumer_copies`, ADR-0013;
  SANDLOOM-T018 when a copy cannot be generated or verified);
- ``<shared>/<file>``: each file a loaded module declares, rendered from the
  composed app's data (:mod:`sandloom.core.cli.generation_input`, SANDLOOM-T016).

Consumer files (stubs, DTO copies) carry a provenance header whose digest
covers the body, and are laid out by the generator itself, never by the
project's formatter (ADR-0013 decisions 6, 7). An application's shared
package holds copies for the union of its own modules' ``consumes`` when every
entry has a method list, else for every composed provider.

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
import importlib.util
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

import sandloom.core.cli.consumer_copies as _consumer_copies
import sandloom.core.cli.dto_copies as _dto_copies
import sandloom.core.cli.generation_input as _generation_input
import sandloom.core.cli.inspection.generation as _inspection_generation
import sandloom.core.cli.route_weakening as _route_weakening
import sandloom.core.contract_compat as _contract_compat
import sandloom.core.route_snapshots as _route_snapshots
import sandloom.core.testing.settings_gate as _settings_gate
from sandloom.core.archcheck.manifest import load_rules
from sandloom.core.composition import load_composition_app
from sandloom.core.contracts.schema_compatibility import (
    schema_problems as schema_problems,
)
from sandloom.core.descriptors.module_names import module_name
from sandloom.core.module_api import contract_schema

#: File name of a module's generated surface snapshot.
MODULE_SNAPSHOT = "module.snapshot.json"
HEADER = _dto_copies.HEADER


@dataclass
class Generated:
    """One generated file: its target, content and verdict against the
    committed version."""

    path: Path
    content: str
    code: str
    problems: list[str] = field(default_factory=list)
    #: a file to delete (an orphan consumer copy, ADR-0013 decision 2)
    remove: bool = False

    @property
    def current(self) -> str | None:
        return self.path.read_text(encoding="utf-8") if self.path.is_file() else None

    @property
    def stale(self) -> bool:
        if self.remove:
            return self.path.is_file()
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
        self.comments: dict[str, str] = {}

    def need(self, module: str, name: str, comment: str | None = None) -> str:
        """Import ``name`` from ``module``; ``comment`` trails its line."""
        self.names.setdefault(module, set()).add(name)
        if comment:
            self.comments[module] = comment
        return name

    def render(self) -> str:
        """isort order: the standard library, then everything else."""
        groups: list[list[str]] = [[], []]
        for module in sorted(self.names):
            names = ", ".join(sorted(self.names[module]))
            comment = f"  # {self.comments[module]}" if module in self.comments else ""
            line = f"from {module} import {names}{comment}"
            if len(line) > _LINE:
                inner = "".join(f"    {n},\n" for n in sorted(self.names[module]))
                line = f"from {module} import ({comment}\n{inner})"
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


#: Where a generated atom imports its evaluator and ``Account`` from: the
#: public authorization facade, marked as advanced generated code (T009).
ATOM_HOME = ("sandloom.sdk.authz", "sandloom: advanced generated code")


def _py(value: Any, indent: str) -> str:
    """A JSON value as Python source at ``indent`` (ruff's layout: inline when
    it fits the line, else one element per line with a trailing comma)."""
    if isinstance(value, dict):
        items = [
            f"{json.dumps(str(k))}: {_py(v, indent + '    ')}" for k, v in value.items()
        ]
        brackets = ("{", "}")
    elif isinstance(value, (list, tuple)):
        items = [_py(v, indent + "    ") for v in value]
        brackets = ("[", "]")
    else:
        return _literal(value)
    flat = brackets[0] + ", ".join(items) + brackets[1]
    if "\n" not in flat and len(indent) + len(flat) <= _LINE:
        return flat
    inner = "".join(f"{indent}    {item},\n" for item in items)
    return f"{brackets[0]}\n{inner}{indent}{brackets[1]}"


def _literal_block(literal: Mapping[str, Any]) -> str:
    """``__sandloom_atom__`` as a class attribute, one key per line (its
    trailing comma keeps ruff from joining it); a value that overflows its
    line is split one element per line, as ruff splits it."""
    lines: list[str] = []
    indent = "        "
    for key, value in literal.items():
        head = f"{indent}{json.dumps(key)}: "
        flat = _py(value, "")
        if "\n" not in flat and len(head) + len(flat) + 1 <= _LINE:
            lines.append(f"{head}{flat},")
        else:
            lines.append(f"{head}{_py(value, indent)},")
    body = "\n".join(lines)
    return f"    __sandloom_atom__: ClassVar[dict[str, Any]] = {{\n{body}\n    }}\n"


def _def_init(params: list[str], call: str) -> str:
    """``def __init__(...) -> None:`` and its ``super().__init__`` body."""
    head: list[str] = []
    for line in _dto_copies.fit_lines(
        f"    def __init__({', '.join(params)}) -> None:"
    ):
        stripped = line.lstrip(" ")
        if stripped.startswith("] | "):
            # A split subscript followed by ``| X``: ruff breaks before the
            # operator, at the bracket's indentation.
            indent = line[: len(line) - len(stripped)]
            head += [f"{indent}]", f"{indent}{stripped[2:]}"]
        else:
            head.append(line)
    body = _dto_copies.fit_lines(f"        super().__init__({call})")
    return "\n".join([*head, *body]) + "\n"


def atom_literal(contract: str, method: str, runtime: Any) -> dict[str, Any]:
    """The ``__sandloom_atom__`` literal of ``runtime``'s access check: what
    the generic evaluator reads (ADR-0014 decision 10)."""
    spec = runtime.access_check
    literal: dict[str, Any] = {
        "contract": contract,
        "method": method,
        "args": list(runtime.signature.parameters),
        "binds": spec["bind"],
        "tier": 3,
        "category": spec.get("category", "forbidden"),
    }
    for key in ("values", "risk", "describe"):
        if spec.get(key):
            literal[key] = spec[key]
    if isinstance(spec.get("witnesses_membership"), Mapping):
        # The membership witness (ADR-0014 decision 13); ``{}`` is a witness
        # with no own parameter, so presence decides, not truthiness.
        literal["witnesses_membership"] = dict(spec["witnesses_membership"])
    return literal


def _class_head(name: str, base: str, doc: str | None) -> str:
    text = docstring(doc, "    ")
    return f"class {name}({base}):\n" + (f"{text}\n\n" if text else "")


def _render_check(
    contract: str,
    method: str,
    runtime: Any,
    doc: str | None,
    imports: _Imports,
    resolver: _dto_copies.Resolver | None,
) -> str:
    """The typed ``ContractCheck`` subclass of one access check: the bound
    parameters leave the signature, the rest are the atom's own (a closed
    ``values`` set typed as a ``Literal``); ``org_param=`` for an organization
    bind, a required ``of=`` for an account bind."""
    spec = runtime.access_check
    binds = spec["bind"]
    kinds = {source[0] for source in binds.values()}
    home, marker = ATOM_HOME
    imports.need("typing", "Any")
    imports.need("typing", "ClassVar")
    imports.need(home, "ContractCheck", marker)
    params, passed = _own_params(runtime, imports, resolver)
    keywords: list[str] = []
    if "org" in kinds:
        default = next(b[1] for b in binds.values() if b[0] == "org")
        params += ([] if "*" in params else ["*"]) + [
            f"org_param: str | None = {_literal(default)}"
        ]
        keywords.append("org_param=org_param")
    if kinds & {"account_type", "account_id"}:
        imports.need(home, "Account", marker)
        params += ([] if "*" in params else ["*"]) + ["of: Account"]
        keywords.append("of=of")
    call = "{" + ", ".join(f"{json.dumps(p)}: {p}" for p in passed) + "}"
    return (
        _class_head(spec["name"], "ContractCheck", doc)
        + _literal_block(atom_literal(contract, method, runtime))
        + "\n"
        + _def_init(params, ", ".join([call, *keywords]))
    )


def _own_params(
    runtime: Any, imports: _Imports, resolver: _dto_copies.Resolver | None
) -> tuple[list[str], list[str]]:
    """The access check's own parameters (every unbound one, in signature
    order, a closed ``values`` set typed as a ``Literal``): the rendered
    ``__init__`` parameters from ``self`` on, and the names passed through."""
    spec = runtime.access_check
    binds = spec["bind"]
    values = spec.get("values") or {}
    params = ["self"]
    passed: list[str] = []
    for pname, param in runtime.signature.parameters.items():
        if pname in binds:
            continue
        if pname in values:
            imports.need("typing", "Literal")
            text = f"Literal[{', '.join(_literal(v) for v in values[pname])}]"
        else:
            text = render_annotation(runtime.annotations[pname], imports, resolver)
        if param.kind is param.KEYWORD_ONLY and "*" not in params:
            params.append("*")
        text = f"{pname}: {text}"
        if param.default is not param.empty:
            text += f" = {_literal(param.default)}"
        params.append(text)
        passed.append(pname)
    return params, passed


def _render_claim(
    spec: Mapping[str, Any], otherwise: dict[str, Any], imports: _Imports
) -> str:
    """The typed ``ClaimFactCheck`` subclass an owner's fact names
    (``access_check(..., claim=fact)``): ``statuses=`` typed as a ``Literal``
    of the declared statuses, ``claim_key=`` of the declared keys (ADR-0014
    decision 11), ``of=`` required."""
    home, marker = ATOM_HOME
    for name in ("Any", "ClassVar", "Literal"):
        imports.need("typing", name)
    imports.need(home, "ClaimFactCheck", marker)
    imports.need(home, "Account", marker)
    literal: dict[str, Any] = {
        "fact": spec["fact"],
        "owner": spec["owner"],
        "claim_key": spec["claim_key"],
        "claim_keys": list(spec["claim_keys"]),
        "statuses": list(spec["statuses"]),
        "tier": 1,
        "category": spec["category"],
        # The owner's declaration for its primary claim proven absent; a
        # spec without one (an older owner) declares "unknown".
        "absent_claim": spec.get("absent_claim") or "unknown",
    }
    if spec.get("describe"):
        literal["describe"] = spec["describe"]
    literal["otherwise"] = otherwise
    keys = ", ".join(_literal(k) for k in spec["claim_keys"])
    statuses = (
        f"frozenset[Literal[{', '.join(_literal(s) for s in spec['statuses'])}]]"
        if spec["statuses"]
        else "frozenset[str]"
    )
    doc = (
        f"The owner's claim ``{spec['claim_key']}`` under its provenance (zero\n"
        "I/O) for ``Account.user()``; for an organization account the live check\n"
        f"``{otherwise['contract']}.{otherwise['method']}``. Fact\n"
        f"``{spec['fact']}`` of ``{spec['owner']}``: indeterminate 501 when that\n"
        "module is neither loaded nor bound."
    )
    params = [
        "self",
        f"statuses: {statuses} | None = None",
        "*",
        "of: Account",
        f"claim_key: Literal[{keys}] = {_literal(spec['claim_key'])}",
    ]
    return (
        _class_head(spec["name"], "ClaimFactCheck", doc)
        + _literal_block(literal)
        + "\n"
        + _def_init(params, "statuses, of=of, claim_key=claim_key")
    )


def _render_claim_read(
    spec: Mapping[str, Any],
    otherwise: dict[str, Any],
    runtime: Any,
    imports: _Imports,
    resolver: _dto_copies.Resolver | None,
) -> str:
    """The typed ``ClaimReadCheck`` subclass a ``claim_atom`` declares: the
    access check's own parameters, ``of=`` required, ``claim_key=`` typed as
    a ``Literal`` of the declared keys (ADR-0014 decision 15)."""
    home, marker = ATOM_HOME
    for name in ("Any", "ClassVar", "Literal"):
        imports.need("typing", name)
    imports.need(home, "ClaimReadCheck", marker)
    imports.need(home, "Account", marker)
    literal: dict[str, Any] = {
        "read": spec["read"],
        "claim_key": spec["claim_key"],
        "claim_keys": list(spec["claim_keys"]),
        "entry": spec["entry"],
    }
    if spec.get("below"):
        literal["below"] = spec["below"]
    literal["tier"] = 1
    literal["category"] = spec["category"]
    if spec.get("describe"):
        literal["describe"] = spec["describe"]
    literal["otherwise"] = otherwise
    entry = f"the entry named by ``{spec['entry']}``"
    test = (
        f"``{spec['below']}`` must be strictly below\n{entry}"
        if spec.get("below")
        else f"{entry} must be\n``True``"
    )
    doc = (
        f"Reads the claim ``{spec['claim_key']}`` for ``Account.user()`` with "
        f"zero I/O:\n{test}. An organization account asks the live check\n"
        f"``{otherwise['contract']}.{otherwise['method']}`` with the same\n"
        "parameters."
    )
    params, passed = _own_params(runtime, imports, resolver)
    keys = ", ".join(_literal(k) for k in spec["claim_keys"])
    params += [
        *([] if "*" in params else ["*"]),
        "of: Account",
        f"claim_key: Literal[{keys}] = {_literal(spec['claim_key'])}",
    ]
    call = "{" + ", ".join(f"{json.dumps(p)}: {p}" for p in passed) + "}"
    return (
        _class_head(spec["name"], "ClaimReadCheck", doc)
        + _literal_block(literal)
        + "\n"
        + _def_init(params, f"{call}, of=of, claim_key=claim_key")
    )


def _render_atoms(
    contract: str,
    provider: Any,
    imports: _Imports,
    resolver: _dto_copies.Resolver | None,
) -> list[str]:
    """The generated atoms of ``provider``'s access checks, by method name,
    then the claim atoms they name."""
    atoms: list[str] = []
    claims: list[tuple[dict[str, Any], dict[str, Any], Any]] = []
    for name, runtime in sorted(contract_schema(provider).methods.items()):
        if runtime.access_check is None:
            continue
        method = getattr(provider, name, None)
        doc = inspect.getdoc(method) if method is not None else None
        atoms.append(_render_check(contract, name, runtime, doc, imports, resolver))
        claim = runtime.access_check.get("claim")
        if isinstance(claim, dict):
            claims.append((claim, atom_literal(contract, name, runtime), runtime))
    for claim, otherwise, runtime in sorted(claims, key=lambda c: str(c[0]["name"])):
        if "read" in claim:
            atoms.append(
                _render_claim_read(claim, otherwise, runtime, imports, resolver)
            )
        else:
            atoms.append(_render_claim(claim, otherwise, imports))
    return atoms


def render_stub(
    contract: str, provider: Any, resolver: _dto_copies.Resolver | None = None
) -> str:
    """The generated stub of contract ``contract`` provided by ``provider``
    (its DTOs imported from where ``resolver`` places them), followed by the
    typed atoms of its access checks (ADR-0014 decision 8)."""
    _stem, protocol, ref = stub_names(contract)
    imports = _Imports()
    imports.need("typing", "Protocol")
    home, marker = _dto_copies.contract_refs_home()
    imports.need(home, "ContractRef", marker)
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
    atoms = _render_atoms(contract, provider, imports, resolver)
    tail = "".join(f"\n\n{atom}" for atom in atoms)
    return (
        f'{HEADER}"""Contract ``{contract}``: the typed stub callers import.\n\n'
        f"Generated from ``{provider.__module__}.{provider.__qualname__}``.\n"
        f'"""\n\n'
        "from __future__ import annotations\n\n"
        f"{imports.render()}\n\n\n"
        f"class {protocol}(Protocol):\n{body}\n\n\n"
        f"{declaration}\n{tail}"
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


#: The stub renderer as :mod:`sandloom.core.cli.consumer_copies` uses it.
STUBS = _consumer_copies.Stubs(render_stub, stub_problems)


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


def _record_settings(
    doc: dict[str, Any], module: str, path: Path, records: Iterable[Any]
) -> None:
    """Add the module's declared settings to its snapshot ``doc`` (ADR-0015
    decision 5-6): each env name with its tier, and which unprefixed names the
    snapshot grandfathers (``settings_gate.settings_section``). ``records``:
    the composed application's validated module settings. A module that
    declares none gets no ``settings`` key."""
    model = next((r.model for r in records if r.package == module), None)
    if model is None:
        return
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = None
    doc[_settings_gate.SNAPSHOT_KEY] = _settings_gate.settings_section(
        model, module_name(module), previous
    )


def module_surface(entry: Any) -> dict[str, Any]:
    """The ``module.snapshot.json`` document of one module manifest entry.

    A token issuer's document also records its ``issuer`` declaration
    (``refresh_sessions``: whether it holds refresh sessions an emergency
    block must end; ADR-0012 decision 5). Other modules carry no such key.
    ``capabilities`` are the infrastructure needs the load preflight checks,
    declared or derived from the module's code (SANDLOOM-C033).
    """
    surface: dict[str, Any] = {
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
        "capabilities": [
            {
                "capability": c.capability,
                "instance": c.instance,
                "version": c.version,
                "features": list(c.features),
                "required": c.required,
            }
            for c in getattr(entry, "capabilities", ())
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
    if getattr(entry, "issues_tokens", False):
        surface["issuer"] = {
            "refresh_sessions": bool(getattr(entry, "refresh_sessions", True))
        }
    return surface


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
    return (_dto_copies.KERNEL_SHARED, *own, *extra)


def _contract_files(
    entry: Any,
    facade: Any,
    shared_dir: Path,
    shared_package: str,
    shared_packages: tuple[str, ...],
    format_dto: Callable[[str, Path], str] | None = None,
    *,
    wanted: set[str] | None = None,
) -> list[Generated]:
    """The stubs of the contracts ``entry`` provides (only those in
    ``wanted`` when given) and, when their signatures name the provider's
    own DTOs, the module's generated DTO copies (one ``<module>_dto.py``,
    so two contracts share one class), each under its provenance header."""
    short = module_name(entry.name)
    label = _consumer_copies.distribution_label(entry.name)
    providers: list[_consumer_copies.Provider] = []
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
        if wanted is not None and provided.name not in wanted:
            continue
        stem, _protocol, _ref = stub_names(provided.name)
        path = shared_dir / "contracts" / f"{stem}.py"
        provider = _consumer_copies.Provider(provided.name, impl, entry.name, label)
        providers.append(provider)
        try:
            content = _consumer_copies.stub_file(
                provided.name, provider, resolver, STUBS
            )
        except _dto_copies.CopyError as exc:
            refused.append(f"{provided.name}: {exc}")
            continue
        stub = Generated(path, content, "SANDLOOM-T007")
        stub.problems = stub_problems(stub.current, stub.content)
        out.append(stub)
        pending_stubs.append((stub, provided.name, provider))
    if resolver.copied or refused:
        try:
            text = _consumer_copies.dto_file(short, resolver, providers)
            if format_dto is not None:
                text = format_dto(text, dto_path)
        except _dto_copies.CopyError as exc:
            refused.append(str(exc))
            text = ""
        copies = Generated(dto_path, text, _dto_copies.CODE)
        copies.problems = refused
        out.append(copies)
        # All contracts and nested/default DTOs are now known, so every stub
        # uses the same final names as the shared DTO module.
        if not refused:
            for stub, name, provider in pending_stubs:
                stub.content = _consumer_copies.stub_file(
                    name, provider, resolver, STUBS
                )
                stub.problems = stub_problems(stub.current, stub.content)
    return out


def _declared_files(app: Any, shared_dir: Path) -> list[Generated]:
    """The files the loaded modules declare (the console's manifest among them).
    Only a process that composes every module can write them."""
    out = []
    for path, content, problems in _generation_input.planned(app, shared_dir):
        item = Generated(path, content, "SANDLOOM-T016")
        item.problems += problems
        out.append(item)
    return out


def _selected(entry: Any, modules: Iterable[str] | None) -> bool:
    if modules is None:
        return True
    wanted = set(modules)
    return entry.name in wanted or module_name(entry.name) in wanted


def _stem_collisions(facade: Any, shared_dir: Path | None) -> list[Generated]:
    """Two contracts of an application mapping to one stub file (refused)."""
    if shared_dir is None:
        return []
    return [
        Generated(
            shared_dir / "contracts" / f"{stem}.py",
            "",
            "SANDLOOM-T007",
            [_consumer_copies.collision_problem(stem, names)],
        )
        for stem, names in sorted(
            _consumer_copies.stem_collisions(facade.service_registry.list()).items()
        )
    ]


def plan(
    app: Any,
    shared_dir: Path | None,
    source_root: Path | None = None,
    *,
    shared_package: str | None = None,
    modules: Iterable[str] | None = None,
    format_dto: Callable[[str, Path], str] | None = None,
    consumed_only: bool = False,
) -> list[Generated]:
    """Every generated file of the app's L1 modules, with its verdict.

    The application's consumer side (contract stubs, DTO copies, exported
    event schemas) is generated for every composed module into ``shared_dir``
    (the package ``shared_package``); a tree of top-level module packages has
    none (``shared_dir`` is ``None``: each module's copies are planned by
    :mod:`sandloom.core.cli.consumer_copies`, the kernel ships no generated
    stub or DTO copy, ADR-0013). The module side (``module.snapshot.json``, route
    and contract snapshots) only for modules under ``source_root``: an
    installed module package ships its own. ``modules`` (names or import
    paths) keeps those modules' files only. DTO modules are rendered in
    their final layout (``format_dto``, a test seam, post-processes one).
    ``consumed_only`` (an application): the shared package holds copies for
    the union of the owned modules' ``consumes`` when every entry lists its
    methods (ADR-0013 decision 2), else for every composed provider."""
    manifest = app.state.app_manifest
    facade = app.state.core
    contracts = _contract_compat.module_snapshots(facade)
    shared_packages = _shared_packages(facade, shared_package)
    out = _stem_collisions(facade, shared_dir)
    wanted = _consumed_union(manifest, source_root) if consumed_only else None
    for entry in manifest.modules:
        if not is_l1(entry.name) or not _selected(entry, modules):
            continue
        directory = _module_dir(entry.name)
        if directory is None:
            continue
        owned = _owned(directory, source_root)
        doc = module_surface(entry)
        _record_settings(
            doc,
            entry.name,
            directory / MODULE_SNAPSHOT,
            facade.module_settings.records(),
        )
        snapshot = Generated(directory / MODULE_SNAPSHOT, _dump(doc), "SANDLOOM-T005")
        old_doc = json.loads(snapshot.current) if snapshot.current else {}
        for channel, schema in doc["events"].items():
            previous = (old_doc.get("events") or {}).get(channel)
            snapshot.problems += [
                f"{channel}: {p}" for p in schema_problems(previous, schema)
            ]
            if shared_dir is None:
                continue
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
        if shared_dir is not None and shared_package is not None:
            out += _contract_files(
                entry,
                facade,
                shared_dir,
                shared_package,
                shared_packages,
                format_dto,
                wanted=wanted,
            )
    if modules is None and shared_dir is not None:
        out += _declared_files(app, shared_dir)
    return out


def _consumed_union(manifest: Any, source_root: Path | None) -> set[str] | None:
    """The contracts the application's own modules consume, or ``None``
    when one entry has no method list (a call the generator could not
    resolve: the per-provider copies stay until ``consumes=`` states it)."""
    union: set[str] = set()
    for entry in manifest.modules:
        directory = _module_dir(entry.name) if is_l1(entry.name) else None
        if directory is None or not _owned(directory, source_root):
            continue
        for use in entry.contracts_consumed:
            if _consumer_copies.is_kernel_family(use.name):
                continue
            if not use.methods:
                return None
            union.add(use.name)
    return union


def _distribution_modules(rules: Any) -> dict[str, Path]:
    """``{package: directory}`` of the module distributions of a tree of
    top-level module packages (none in an application)."""
    if not rules.top_level_modules:
        return {}
    return {
        rules.module_package(name): rules.modules_path / name
        for name in rules.business_modules()
    }


def _consumer_plan(
    app: Any, rules: Any, planned: dict[str, set[str]], modules: Iterable[str] | None
) -> list[Generated]:
    """The module distributions' copies after composition: expectations
    narrowed to the composed ``consumes``, which must equal the planned set."""
    tree = _distribution_modules(rules)
    consumes: dict[str, dict[str, tuple[str, ...]]] = {}
    for entry in app.state.app_manifest.modules:
        if entry.name in tree and _selected(entry, modules):
            consumes[entry.name] = {
                u.name: tuple(u.methods) for u in entry.contracts_consumed
            }
    selected = {p: d for p, d in tree.items() if p in consumes}
    registry = {}
    facade = app.state.core
    for name in facade.service_registry.list():
        impl = facade.service_registry.try_get(name)
        impl = getattr(impl, "contract_impl", impl)
        owner = facade.contract_owner(name)
        if impl is not None and owner and hasattr(impl, "__sandloom_contract__"):
            label = _consumer_copies.distribution_label(owner)
            registry[name] = _consumer_copies.Provider(name, impl, owner, label)
    files = _consumer_copies.plan_composed(
        selected, consumes, planned, rules.source_root, STUBS, registry
    )
    return [Generated(f.path, f.content, f.code, f.problems, f.remove) for f in files]


def _ensure_package(directory: Path, shared_dir: Path | None) -> None:
    """``__init__.py`` in the shared package and its ``contracts`` package."""
    for package in (directory,) if shared_dir is None else (shared_dir, directory):
        init = package / "__init__.py"
        if package.is_dir() and not init.exists():
            init.write_text(
                '"""Contract DTO package (generated stubs and DTO copies)."""\n',
                encoding="utf-8",
            )


#: The fix printed per incompatible-change code.
_FIXES = {
    "SANDLOOM-T016": "fix the declaration of the module named above",
    "SANDLOOM-T001": "a weaker route policy is written only once approved: review "
    "it, then run `sandloom routes snapshot --write` (the snapshot diff records the "
    "approval)",
    "SANDLOOM-T004": "a breaking contract change is a new version (.v2)",
    "SANDLOOM-T005": "a breaking change is a new version (.v2)",
    "SANDLOOM-T007": "a breaking contract change is a new version (.v2)",
    "SANDLOOM-T018": "install the provider as a development dependency or commit "
    "its generated copy, and state each consumed contract's methods in "
    "define_module(consumes=[...]) where a call cannot be resolved",
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


def _why_stale(item: Generated) -> str:
    if item.remove:
        return "(orphan: no longer consumed)"
    state = "missing" if item.current is None else "differs"
    return f"({state}){_consumer_copies.explain_diff(item.current, item.content)}"


def _write(item: Generated, shared_dir: Path | None) -> None:
    item.path.parent.mkdir(parents=True, exist_ok=True)
    if item.path.suffix == ".py":
        _ensure_package(item.path.parent, shared_dir)
    if item.path.name == "inspection.json" and item.path.parent.name == ".sandloom":
        _inspection_generation.publish_index(item.path, item.content)
    else:
        item.path.write_text(item.content, encoding="utf-8")


def apply(
    files: Iterable[Generated],
    *,
    write: bool,
    shared_dir: Path | None,
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
            print(f"  {_shown(item.path, root)} [{item.code}] {_why_stale(item)}")
        return 1 if stale else 0
    for item in stale:
        if item.remove:
            item.path.unlink()
            print(f"removed {_shown(item.path, root)} (no longer consumed)")
        else:
            _write(item, shared_dir)
            print(f"wrote {_shown(item.path, root)}")
    return 0


def _agent_reference(root: Path) -> list[Generated]:
    """The framework repository's agent reference (``tools/agent_reference.py``
    renders ``.claude/agent-reference.md``); an application has no such script."""
    script = root / "tools" / "agent_reference.py"
    if not script.is_file():
        return []
    spec = importlib.util.spec_from_file_location("sandloom_agent_reference", script)
    if spec is None or spec.loader is None:
        return []
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [Generated(root / module.OUT, module.render(root), "SANDLOOM-T005")]


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
    tree = _distribution_modules(rules)
    if args.modules:
        tree = {
            p: d
            for p, d in tree.items()
            if p in args.modules or module_name(p) in args.modules
        }
    early, planned = _consumer_copies.plan_static(tree, rules.source_root, STUBS)
    pre = [Generated(f.path, f.content, f.code, f.problems) for f in early]
    if any(f.problems for f in pre) or (args.check and any(f.stale for f in pre)):
        return apply(
            pre, write=not args.check, shared_dir=rules.shared_dir, root=rules.root
        )
    if not args.check and any(f.stale for f in pre):
        # Written before composition: a module may import a copy it has
        # not generated yet (ADR-0013 decision 2).
        apply(pre, write=True, shared_dir=rules.shared_dir, root=rules.root)
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
    consumed_only = not rules.top_level_modules
    files = plan(
        app,
        rules.shared_dir,
        rules.source_root,
        shared_package=rules.shared_dto_package,
        modules=args.modules or None,
        # A named provider can publish its first stub before any caller exists.
        consumed_only=consumed_only and not args.modules,
    ) + _consumer_plan(app, rules, planned, args.modules or None)
    if not args.modules:
        files += _agent_reference(rules.root)
    return _inspection_generation.finish(
        rules=rules,
        app=app,
        selected=files,
        modules=args.modules,
        check=args.check,
        before=before,
        plan_all=lambda: (
            plan(
                app,
                rules.shared_dir,
                rules.source_root,
                shared_package=rules.shared_dto_package,
                consumed_only=consumed_only,
            )
            + _consumer_plan(app, rules, planned, None)
        ),
        make_file=Generated,
        apply_files=apply,
    )


__all__ = [
    "HEADER",
    "MODULE_SNAPSHOT",
    "STALE",
    "STUBS",
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
