"""``sandloom docs`` for what an app calls besides ``sandloom.sdk`` names: the
contracts and the event schemas of the installed modules and of the application
(spec §5.2, §5.3; ADR-0013).

``sandloom docs contracts`` / ``sandloom docs events`` list them; ``sandloom docs
billing.credits.v1`` (or ``BILLING_CREDITS_V1``) prints each method's
signature and the provider's docstring; ``sandloom docs tenancy.member_added.v2``
prints the event's fields, the producer's notes and a view to copy.

The kernel ships no generated stub or schema. A contract is read from its
provider (the installed module whose ``module.snapshot.json`` lists it, its
``contracts`` module imported only to describe one) or, in an application, from
the stub in its own ``<app>.shared`` package; an event schema from the
producer's ``module.snapshot.json`` or the application's exported file. No
repository ``docs/`` is needed.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import json
import pkgutil
import re
import textwrap
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sandloom.core.descriptors.module_names import installed_modules


def _app_shared() -> str | None:
    """``<app>.shared`` of the application in the working directory."""
    pyproject = Path.cwd() / "pyproject.toml"
    try:
        name = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["name"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        return None
    package = f"{re.sub(r'[^a-z0-9_]', '_', str(name).lower())}.shared"
    try:
        found = importlib.util.find_spec(package)
    except (ImportError, ValueError):
        return None
    return package if found is not None else None


def _package_dir(package: str) -> Path | None:
    try:
        spec = importlib.util.find_spec(package)
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(next(iter(spec.submodule_search_locations)))


def _snapshots() -> list[tuple[str, dict[str, Any]]]:
    """``(import path, module.snapshot.json)`` of every installed module that
    ships one (the packages are located, not imported)."""
    found: list[tuple[str, dict[str, Any]]] = []
    for installed in installed_modules():
        directory = _package_dir(installed.path)
        if directory is None:
            continue
        try:
            doc = json.loads((directory / "module.snapshot.json").read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict):
            found.append((installed.path, doc))
    return found


# --------------------------------------------------------------------------- #
# Contracts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Stub:
    name: str  # billing.credits.v1
    module: str  # the module a caller imports the reference from
    ref: str  # BILLING_CREDITS_V1
    #: The application stub's file; ``None`` for an installed provider.
    path: Path | None = None
    #: The installed provider's import path (its ``contracts`` module is read).
    provider: str | None = None


def _ref_of(name: str) -> tuple[str, str]:
    stem = "_".join(name.split("."))
    return stem, stem.upper()


def contract_stubs() -> dict[str, _Stub]:
    """``{contract name: stub}`` of every contract found: the application's
    own stubs first, then each installed provider's contracts."""
    found: dict[str, _Stub] = {}
    app = _app_shared()
    directory = _package_dir(f"{app}.contracts") if app else None
    if app and directory is not None:
        for info in pkgutil.iter_modules([str(directory)]):
            stub = _read_stub(f"{app}.contracts.{info.name}", directory)
            if stub is not None:
                found.setdefault(stub.name, stub)
    for package, doc in _snapshots():
        for entry in doc.get("contracts") or []:
            name = entry.get("name") if isinstance(entry, dict) else None
            if isinstance(name, str) and name not in found:
                stem, ref = _ref_of(name)
                found[name] = _Stub(
                    name,
                    f"<your_module>._generated.contracts.{stem}",
                    ref,
                    provider=package,
                )
    return dict(sorted(found.items()))


_REF = re.compile(
    r'^([A-Z][A-Z0-9_]*): ContractRef\[\w+\] = ContractRef\(\s*"([^"]+)"', re.M
)


def _read_stub(module: str, directory: Path) -> _Stub | None:
    path = directory / f"{module.rsplit('.', 1)[-1]}.py"
    try:
        match = _REF.search(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    if match is None:
        return None
    return _Stub(name=match.group(2), module=module, ref=match.group(1), path=path)


def _find_stub(name: str) -> _Stub | None:
    stubs = contract_stubs()
    wanted = name.strip()
    for stub in stubs.values():
        if wanted in (stub.name, stub.ref, stub.module.rsplit(".", 1)[-1], stub.module):
            return stub
    return None


def _annotation(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, type) and not getattr(value, "__args__", None):
        return value.__name__
    text = str(value).replace("typing.", "")
    return re.sub(r"\b(?:\w+\.)+(\w+)", r"\1", text)


def _signature(function: Any) -> tuple[str, str]:
    """``(arguments, " -> return")`` of a provider method, as a caller writes it."""
    signature = inspect.signature(function)
    arguments = []
    for param in signature.parameters.values():
        text = param.name
        if param.annotation is not inspect.Parameter.empty:
            text += f": {_annotation(param.annotation)}"
        if param.default is not inspect.Parameter.empty:
            text += f" = {param.default!r}"
        arguments.append(text)
    returns = ""
    if signature.return_annotation is not inspect.Signature.empty:
        returns = f" -> {_annotation(signature.return_annotation)}"
    return ", ".join(arguments), returns


def _provider_class(stub: _Stub) -> type | None:
    """The ``@contract`` class of ``stub`` in its provider's ``contracts`` module."""
    try:
        module = importlib.import_module(f"{stub.provider}.contracts")
    except ImportError:
        return None
    for value in vars(module).values():
        if not isinstance(value, type):
            continue  # never touch a lazy proxy
        spec = value.__dict__.get("__sandloom_contract__")
        if spec is not None and getattr(spec, "name", None) == stub.name:
            return value
    return None


Method = tuple[str, str, str, str | None]


def _provider_methods(cls: type) -> list[Method]:
    out: list[Method] = []
    for key in vars(cls):
        member = getattr(cls, key)
        if key.startswith("_") or not callable(member):
            continue
        arguments, returns = _signature(member)
        out.append((key, arguments, returns, inspect.getdoc(member)))
    return out


def _stub_methods(stub: _Stub) -> tuple[str | None, list[Method]]:
    """``(class docstring, methods)`` from an application stub's source."""
    if stub.path is None:
        return None, []
    tree = ast.parse(stub.path.read_text(encoding="utf-8"))
    protocol = next((n for n in tree.body if isinstance(n, ast.ClassDef)), None)
    if protocol is None:
        return None, []
    methods: list[Method] = []
    for node in protocol.body:
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        args = ast.unparse(node.args).removeprefix("self, ").removeprefix("self")
        returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        methods.append((node.name, args, returns, ast.get_docstring(node)))
    return ast.get_docstring(protocol), methods


def describe_contract(stub: _Stub) -> str:
    """The ``sandloom docs <contract>`` text: the import, then every method's
    signature and docstring, from the application stub's source or the
    provider's class."""
    out = [f"{stub.name} (contract)"]
    if stub.provider is None:
        class_doc, methods = _stub_methods(stub)
        out.append(f"  from {stub.module} import {stub.ref}")
    else:
        out += [
            f"  provider: {stub.provider}",
            f"  from {stub.module} import {stub.ref}",
            "    (the calling module lists it in define_module(consumes=...) and "
            "`sandloom generate` writes the copy: <module>/_generated/contracts/, "
            "<app>/shared/contracts/ in an application)",
        ]
        cls = _provider_class(stub)
        if cls is None:
            out.append(f"  ({stub.provider}.contracts could not be read)")
            return "\n".join(out)
        class_doc, methods = inspect.getdoc(cls), _provider_methods(cls)
    out.append(
        f"  client = Contract({stub.ref})  # at module level (sandloom.sdk.Contract)"
    )
    if class_doc:
        out += ["", textwrap.indent(class_doc, "  ")]
    for name, args, returns, doc in methods:
        out += ["", f"  await client.{name}({args}){returns}"]
        out.append(textwrap.indent(doc or "(no docstring on the provider)", "      "))
    out += [
        "",
        "  Errors arrive as HTTPException with the provider's status, the same",
        "  locally and over RPC; tests fake the provider with fake_contract.",
        "  More: sandloom docs recipe call-contract",
    ]
    return "\n".join(out)


def list_contracts() -> str:
    stubs = contract_stubs()
    if not stubs:
        return "sandloom docs: no contracts found (no installed module provides one)"
    width = max(len(name) for name in stubs)
    lines = ["contracts (sandloom docs <name> for the methods):"]
    lines += [
        f"  {name:<{width}}  from {stub.module} import {stub.ref}"
        for name, stub in stubs.items()
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


def event_schemas() -> dict[str, tuple[dict[str, Any], str]]:
    """``{channel: (schema, where it was read)}``: the application's exported
    files first, then each installed producer's ``module.snapshot.json``."""
    found: dict[str, tuple[dict[str, Any], str]] = {}
    app = _app_shared()
    directory = _package_dir(app) if app else None
    if directory is not None and (directory / "events").is_dir():
        for path in sorted((directory / "events").glob("*.json")):
            try:
                schema = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            found.setdefault(path.stem, (schema, str(path)))
    for package, doc in _snapshots():
        for channel, schema in sorted((doc.get("events") or {}).items()):
            if isinstance(schema, dict):
                found.setdefault(channel, (schema, f"{package} module.snapshot.json"))
    return dict(sorted(found.items()))


_FORMATS = {"uuid": "UUID", "date-time": "datetime", "date": "date"}
_TYPES = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "null": "None",
}


def _type(spec: dict[str, Any]) -> str:
    if "anyOf" in spec:
        return " | ".join(_type(s) for s in spec["anyOf"])
    if "$ref" in spec:
        return str(spec["$ref"]).rsplit("/", 1)[-1]
    if "enum" in spec:
        return "Literal[" + ", ".join(json.dumps(v) for v in spec["enum"]) + "]"
    if "const" in spec:
        return f"Literal[{json.dumps(spec['const'])}]"
    kind = spec.get("type")
    if kind == "string" and spec.get("format") in _FORMATS:
        return _FORMATS[spec["format"]]
    if kind == "array":
        return f"list[{_type(spec.get('items') or {})}]"
    if kind == "object":
        return "dict[str, Any]"
    return _TYPES.get(str(kind), "Any")


def _field_lines(schema: dict[str, Any]) -> list[tuple[str, str, bool, str]]:
    required = set(schema.get("required") or [])
    rows = []
    for name, spec in (schema.get("properties") or {}).items():
        note = str(spec.get("description") or "")
        if "minimum" in spec:
            note = f"(>= {spec['minimum']}) {note}".strip()
        rows.append((name, _type(spec), name in required, note))
    rows.sort(key=lambda r: not r[2])
    return rows


def describe_event(channel: str, schema: dict[str, Any], source: str) -> str:
    """The ``sandloom docs <channel>`` text: fields, the producer's notes and a
    view of the channel to copy."""
    rows = _field_lines(schema)
    base, version = channel.rsplit(".", 1)
    others = sorted(
        c for c in event_schemas() if c.rsplit(".", 1)[0] == base and c != channel
    )
    out = [
        f"{channel} (event) — producer {schema.get('title', '?')}; schema {source}",
    ]
    if schema.get("description"):
        out += ["", textwrap.indent(str(schema["description"]), "  ")]
    out += ["", "  fields (wire names; required first):"]
    width = max((len(r[0]) for r in rows), default=0)
    twidth = max((len(r[1]) for r in rows), default=0)
    for name, kind, required, note in rows:
        flag = "required" if required else "optional"
        out.append(f"    {name:<{width}}  {kind:<{twidth}}  {flag}  {note}".rstrip())
    names = {r[0]: r for r in rows}
    notes = [f"version {version} of {base}"]
    if others:
        notes[0] += f"; also on {', '.join(others)}"
    tenant = names.get("tenant_id")
    if tenant is not None and tenant[1] == "UUID":
        notes.append(
            "tenant: tenant_id — @on binds it (tenant='derive'); a projection "
            "reads it (SourceGroup tenant_from='tenant_id')"
        )
    else:
        notes.append(
            "tenant: no tenant_id field — @on(View, tenant='<uuid field>') or "
            "tenant=None"
        )
    order = names.get("version")
    if order is not None and order[1] == "int" and order[2]:
        notes.append(
            "order: version (required) — delivery is unordered; order and "
            "dedupe by it (projection version_field='version')"
        )
    elif order is not None:
        notes.append("order: version is optional — a projection cannot use it")
    else:
        notes.append(
            "order: no version field — not a projection source; consume it with "
            "@on and re-read the owner's state"
        )
    out += ["", "  notes:"] + [f"    - {n}" for n in notes]
    title = re.sub(r"V\d+$", "", str(schema.get("title") or "Event")) or "Event"
    view = [
        f'class {title}(Event, channel="{channel}"):  # a view: the fields you read'
    ]
    for name, kind, required, _note in rows:
        default = "" if required else " = None"
        view.append(f"    {name}: {kind}{default}")
    out += ["", "  a view in your module (consumers.py / projections.py):", ""]
    out += [f"    {line}" for line in view]
    out += [
        "",
        "  More: sandloom docs recipe projection-from-platform-events, event-v2",
    ]
    return "\n".join(out)


def list_events() -> str:
    schemas = event_schemas()
    if not schemas:
        return "sandloom docs: no event schemas found (no installed producer)"
    lines = ["events (sandloom docs <channel> for the fields):"]
    for channel, (schema, _source) in schemas.items():
        doc = str(schema.get("description"))
        first = doc.split("\n", 1)[0] if doc and doc != "None" else ""
        lines.append(f"  {channel:<44} {first}".rstrip())
    return "\n".join(lines)


def describe_wire(name: str) -> str | None:
    """``sandloom docs`` text for a contract, an event channel or a listing;
    ``None`` when ``name`` is neither."""
    if name == "contracts":
        return list_contracts()
    if name == "events":
        return list_events()
    schemas = event_schemas()
    if name in schemas:
        return describe_event(name, *schemas[name])
    stub = _find_stub(name)
    if stub is not None:
        return describe_contract(stub)
    return None


__all__: list[str] = []
