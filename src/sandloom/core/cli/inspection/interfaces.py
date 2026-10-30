"""Compact declared interfaces and composition-local users, without source bodies."""

from __future__ import annotations

from sandloom.core.cli.inspection.model import Doc
from sandloom.core.cli.inspection.read import Reader


def type_name(value: Doc) -> str:
    kind = value.get("k")
    if kind == "scalar":
        return str(value["t"])
    if kind in {"model", "ref", "enum"}:
        return str(value.get("ref", "unknown")).rsplit(":", 1)[-1]
    if kind in {"none", "any"}:
        return "None" if kind == "none" else "Any"
    if kind == "union":
        return " | ".join(type_name(t) for t in value["of"])
    if kind in {"list", "set", "frozenset"}:
        return f"{kind}[{type_name(value['item'])}]"
    if kind == "dict":
        return f"dict[{type_name(value['key'])}, {type_name(value['value'])}]"
    if kind == "tuple":
        if value.get("variadic"):
            return f"tuple[{type_name(value['item'])}, ...]"
        members = ", ".join(type_name(t) for t in value.get("items", []))
        return "tuple[" + (members or "()") + "]"
    if kind == "literal":
        return "Literal[" + ", ".join(repr(v) for v in value.get("values", [])) + "]"
    return f"<unavailable type: {kind}>"


def signature(name: str, method: Doc) -> str:
    parameters = []
    keyword = False
    for parameter in method.get("params", []):
        if parameter["kind"] == "keyword_only" and not keyword:
            parameters.append("*")
            keyword = True
        parameters.append(
            f"{parameter['name']}: {type_name(parameter['type'])}"
            + (" = …" if not parameter["required"] else "")
        )
    return f"{name}({', '.join(parameters)}) -> {type_name(method['returns'])}"


def _models(value: object, found: dict[str, Doc]) -> None:
    if isinstance(value, dict):
        if value.get("k") == "model" and "fields" in value:
            reference = value["ref"]
            found[reference] = {
                "ref": reference,
                "fields": [
                    {
                        "name": name,
                        "type": type_name(field["type"]),
                        "required": field["required"],
                    }
                    for name, field in sorted(value["fields"].items())
                ],
            }
        for child in value.values():
            _models(child, found)
    elif isinstance(value, list):
        for child in value:
            _models(child, found)


def _contract(reader: Reader, edge: Doc, focus: bool) -> Doc:
    channel = edge["target"].removeprefix("contract:")
    description = reader.doc.get("contract_descriptions", {}).get(channel, {})
    methods = [
        {
            "kind": method["kind"],
            "signature": signature(name, method),
            **(
                {
                    k: method[k]
                    for k in ("idempotency_key", "idempotency_scope", "risk")
                    if method.get(k)
                }
                if focus
                else {}
            ),
        }
        for name, method in sorted(description.get("methods", {}).items())
    ]
    peers = sorted(
        {
            e["owner"]
            for e in reader.doc["edges"]
            if e["target"] == edge["target"]
            and e["kind"]
            == (
                "requires_contract"
                if edge["kind"] == "provides_contract"
                else "provides_contract"
            )
        }
    )
    result: Doc = {
        "id": edge["target"],
        "name": channel,
        "methods": methods,
        "description_available": bool(description),
        "peers": peers,
    }
    if "required" in edge:
        result["required"] = edge["required"]
    if focus:
        models: dict[str, Doc] = {}
        _models(description, models)
        result["dtos"] = [models[k] for k in sorted(models)]
    return result


def build(reader: Reader, module: Doc, focused: Doc | None) -> Doc:
    selected = focused["item"]["id"] if focused else None
    whole = not focused or focused["all_interfaces"]
    interfaces = set(focused["channels"]) if focused else set()
    if selected and selected.startswith("contract:"):
        interfaces.add(selected)
    if focused:
        for step in focused["item"].get("steps", []):
            interfaces.update(
                "contract:" + step[k].rsplit(".", 1)[0]
                for k in ("call", "compensate")
                if step.get(k)
            )
    edges = [e for e in reader.doc["edges"] if e["owner"] == module["id"]]
    contracts = [
        e
        for e in edges
        if e["kind"] in {"provides_contract", "requires_contract"}
        and (whole or e["target"] in interfaces)
    ]
    produced, consumed = [], []
    nodes = {n["id"]: n for n in reader.doc["nodes"]}
    for edge in edges:
        if edge["kind"] not in {"produces_event", "consumes_event", "projects_event"}:
            continue
        event_id = (
            edge["target"] if edge["kind"] == "produces_event" else edge["source"]
        )
        channel = event_id.removeprefix("event:")
        if not whole and channel not in interfaces:
            continue
        if edge["kind"] == "produces_event":
            produced.append(
                {
                    "channel": channel,
                    "listeners": [
                        {"module": e["owner"], "consumer": nodes[e["target"]]["name"]}
                        for e in reader.doc["edges"]
                        if e["source"] == event_id
                        and e["kind"] in {"consumes_event", "projects_event"}
                    ],
                }
            )
        else:
            consumed.append(
                {
                    "channel": channel,
                    "handler": nodes[edge["target"]]["name"],
                    "kind": edge["kind"],
                }
            )
    result: Doc = {
        "contracts_provided": [
            _contract(reader, e, bool(focused))
            for e in contracts
            if e["kind"] == "provides_contract"
        ],
        "contracts_used": [
            _contract(reader, e, bool(focused))
            for e in contracts
            if e["kind"] == "requires_contract"
        ],
        "events_published": sorted(produced, key=lambda e: e["channel"]),
        "events_consumed": sorted(consumed, key=lambda e: (e["channel"], e["handler"])),
    }
    if focused:
        result["event_schemas"] = {
            channel: schema
            for channel, schema in reader.doc.get("event_schemas", {}).items()
            if channel in interfaces
        }
    return result


def routes(rows: list[Doc]) -> list[Doc]:
    """Group namespace/resource prefixes, retaining every method and relative path."""
    return _route_groups(sorted(rows, key=lambda r: (r["path"], r["method"])), ())


def _route_groups(rows: list[Doc], prefix: tuple[str, ...]) -> list[Doc]:
    children: dict[str, list[Doc]] = {}
    for row in rows:
        parts = row["path"].strip("/").split("/")
        if len(parts) > len(prefix) and parts[len(prefix)]:
            children.setdefault(parts[len(prefix)], []).append(row)
    version = bool(prefix and prefix[-1].startswith("v") and prefix[-1][1:].isdigit())
    eligible = {
        k: v
        for k, v in children.items()
        if len(v) > 1
        and "{" not in k
        and (
            not prefix
            or version
            or any(len(r["path"].strip("/").split("/")) > len(prefix) + 1 for r in v)
        )
    }
    if len(prefix) >= 2 and not version:
        eligible = {}
    nested = []
    claimed = {id(r) for values in eligible.values() for r in values}
    for key, values in sorted(eligible.items()):
        nested += _route_groups(values, (*prefix, key))
    remaining = [r for r in rows if id(r) not in claimed]
    if remaining:
        path = "/" + "/".join(prefix)
        nested.insert(
            0,
            {
                "prefix": path,
                "routes": [
                    {
                        "method": r["method"],
                        "path": (r["path"][len(path) :] or "/")
                        if prefix
                        else r["path"],
                        "handler": r.get("name"),
                        "auth": r.get("auth"),
                        "source": r.get("source"),
                        "full_path": r["path"],
                    }
                    for r in remaining
                ],
            },
        )
    return nested
