"""``sandloom docs`` for what an app calls besides ``sandloom.sdk`` names: the generated
contract stubs and the exported event schemas (spec §5.2, §5.3).

``sandloom docs contracts`` / ``sandloom docs events`` list them; ``sandloom docs
billing.credits.v1`` (or ``BILLING_CREDITS_V1``) prints each method's
signature and the provider's docstring; ``sandloom docs tenancy.member_added.v2``
prints the event's fields, the producer's notes and a view to copy.

Everything is read from the installed packages: ``sandloom.shared.contracts`` /
``sandloom.shared.events`` (the bundled modules) and, in an application, its own
``<app>.shared`` package (named by ``pyproject.toml`` in the working
directory). No repository ``docs/`` is needed.
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

#: The bundled modules' shared package (stubs and event schemas).
BUNDLED = "sandloom.shared"


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


def _shared_packages() -> list[str]:
    """The shared packages to read: the bundled one first, then the app's."""
    packages = [BUNDLED]
    app = _app_shared()
    if app is not None and app != BUNDLED:
        packages.append(app)
    return packages


def _package_dir(package: str) -> Path | None:
    try:
        spec = importlib.util.find_spec(package)
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(next(iter(spec.submodule_search_locations)))


# --------------------------------------------------------------------------- #
# Contracts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Stub:
    name: str  # billing.credits.v1
    module: str  # sandloom.shared.contracts.billing_credits_v1
    ref: str  # BILLING_CREDITS_V1


def contract_stubs() -> dict[str, _Stub]:
    """``{contract name: stub}`` of every generated stub found (the bundled
    package wins for a stub the app mirrors)."""
    found: dict[str, _Stub] = {}
    for package in _shared_packages():
        directory = _package_dir(f"{package}.contracts")
        if directory is None:
            continue
        for info in pkgutil.iter_modules([str(directory)]):
            stub = _read_stub(f"{package}.contracts.{info.name}", directory)
            if stub is not None and stub.name not in found:
                found[stub.name] = stub
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
    return _Stub(name=match.group(2), module=module, ref=match.group(1))


def _find_stub(name: str) -> _Stub | None:
    stubs = contract_stubs()
    wanted = name.strip()
    for stub in stubs.values():
        if wanted in (stub.name, stub.ref, stub.module.rsplit(".", 1)[-1], stub.module):
            return stub
    return None


def describe_contract(stub: _Stub) -> str:
    """The ``sandloom docs <contract>`` text: the import, then every method's
    signature and docstring, from the stub's source."""
    module = importlib.import_module(stub.module)
    tree = ast.parse(inspect.getsource(module))
    protocol = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef)), None
    )
    out = [
        f"{stub.name} (contract)",
        f"  from {stub.module} import {stub.ref}",
        f"  client = Contract({stub.ref})  # at module level (sandloom.sdk.Contract)",
    ]
    if protocol is None:
        return "\n".join(out)
    class_doc = ast.get_docstring(protocol)
    if class_doc:
        out += ["", textwrap.indent(class_doc, "  ")]
    for node in protocol.body:
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        args = ast.unparse(node.args).removeprefix("self, ").removeprefix("self")
        returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        out += ["", f"  await client.{node.name}({args}){returns}"]
        doc = ast.get_docstring(node)
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
        return "sandloom docs: no generated contract stubs found"
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


def event_schemas() -> dict[str, Path]:
    """``{channel: exported schema file}`` (bundled first, then the app's)."""
    found: dict[str, Path] = {}
    for package in _shared_packages():
        directory = _package_dir(package)
        if directory is None or not (directory / "events").is_dir():
            continue
        for path in sorted((directory / "events").glob("*.json")):
            found.setdefault(path.stem, path)
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


def describe_event(channel: str, path: Path) -> str:
    """The ``sandloom docs <channel>`` text: fields, the producer's notes and a
    view of the channel to copy."""
    schema = json.loads(path.read_text(encoding="utf-8"))
    rows = _field_lines(schema)
    base, version = channel.rsplit(".", 1)
    others = sorted(
        c for c in event_schemas() if c.rsplit(".", 1)[0] == base and c != channel
    )
    out = [
        f"{channel} (event) — producer {schema.get('title', '?')}; schema {path}",
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
        return "sandloom docs: no exported event schemas found"
    lines = ["events (sandloom docs <channel> for the fields):"]
    for channel, path in schemas.items():
        try:
            doc = str(json.loads(path.read_text(encoding="utf-8")).get("description"))
        except (OSError, ValueError):
            doc = ""
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
        return describe_event(name, schemas[name])
    stub = _find_stub(name)
    if stub is not None:
        return describe_contract(stub)
    return None


__all__: list[str] = []
