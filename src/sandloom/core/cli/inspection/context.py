"""Compact module navigation maps; callers open implementation files themselves."""

from __future__ import annotations

from pathlib import Path

from sandloom.core.cli.inspection import files, focus, interfaces, inventory
from sandloom.core.cli.inspection.model import Doc, InspectionError, encode, response
from sandloom.core.cli.inspection.read import Reader

_FRESHNESS = {
    "verified": "verified",
    "source_changed": "source changed since last generate",
    "missing_receipt": "not verified on this checkout",
    "dependency_changed": "declarations or dependencies changed since last generate",
    "partial": "verification incomplete for this scope",
}


def _text(lines: list[str]) -> str:
    clean: list[str] = []
    for line in lines:
        line = "".join(c if c.isprintable() else " " for c in line).rstrip()
        if line or (clean and clean[-1]):
            clean.append(line)
    return "\n".join(clean).rstrip() + "\n"


def _contracts(title: str, entries: list[Doc], provided: bool) -> list[str]:
    out = ["## " + title]
    for entry in entries:
        out.append(
            "- "
            + entry["name"]
            + (" (optional)" if entry.get("required") is False else "")
        )
        for method in entry["methods"]:
            out.append(f"  - {method['kind']} {method['signature']}")
            semantics = "; ".join(
                f"{k}={method[k]}"
                for k in ("idempotency_key", "idempotency_scope", "risk")
                if method.get(k)
            )
            if semantics:
                out.append("    " + semantics)
        if not entry["description_available"]:
            out.append("  - Signature metadata unavailable.")
        peers = ", ".join(p.removeprefix("module:") for p in entry["peers"])
        out.append(
            f"  - {'Callers' if provided else 'Providers'} here: "
            f"{peers or 'none declared'}"
        )
        for dto in entry.get("dtos", []):
            location = (
                f"{dto['path']}:{dto['line']}"
                if dto["path"]
                else "local copy unavailable"
            )
            out.append(f"  - DTO {dto['name']} · {location}")
            out += [
                f"    - {f['name']}: {f['type']}"
                + (" (optional)" if not f["required"] else "")
                for f in dto["fields"]
            ]
    return [*out, ""] if entries else []


def _files(data: Doc) -> list[str]:
    if not data["files"] and not data["file_groups"]:
        return []
    out, root = ["## Files"], data["module"]["source"] + "/"
    for record in data["files"]:
        symbols = ", ".join(f"{s['name']}:{s['line']}" for s in record["symbols"])
        if record["more_symbols"]:
            symbols += f" +{record['more_symbols']}"
        description = record.get("parse_error") or " — ".join(
            p for p in (record["summary"], symbols) if p
        )
        out.append(
            "- "
            + record["path"].removeprefix(root)
            + (" · " + description if description else "")
        )
    for group in data["file_groups"]:
        if group["kind"] == "tests":
            summary = f"{group['count']} " + (
                "file" if group["count"] == 1 else "files"
            )
            if set(group["directories"]) != {"."}:
                summary += " · " + ", ".join(
                    f"{'root' if p == '.' else p}: {n}"
                    for p, n in group["directories"].items()
                )
        elif group["kind"] == "migrations":
            summary = f"{group['revision_count']} revisions; declared heads: " + (
                ", ".join(group["heads"]) or "unavailable"
            )
        else:
            summary = ", ".join(group["files"])
        out.append(f"- {group['path'].removeprefix(root)}/ · {summary}")
    return [*out, ""]


def _published(events: list[Doc]) -> list[str]:
    out = [f"## Events published: {len(events)}"]
    grouped: dict[tuple[str, str], list[str]] = {}
    for event in events:
        parts = event["channel"].rsplit(".", 2)
        if len(parts) == 3:
            grouped.setdefault((parts[0], parts[2]), []).append(parts[1])
        else:
            out.append("- " + event["channel"])
    for (prefix, version), names in sorted(grouped.items()):
        channel = (
            f"{prefix}.{names[0]}.{version}"
            if len(names) == 1
            else f"{prefix}.{{{', '.join(names)}}}.{version}"
        )
        out.append("- " + channel)
    listeners = [e for e in events if e["listeners"]]
    out += [
        "- "
        + e["channel"]
        + " → "
        + ", ".join(
            p["module"].removeprefix("module:") + "/" + p["consumer"]
            for p in e["listeners"]
        )
        for e in listeners
    ]
    if not listeners:
        out.append("Listeners in this composition: none declared.")
    return out


def _interface_paths(paths: list[str]) -> list[str]:
    if not paths:
        return []
    grouped: dict[str, list[str]] = {}
    for path in paths:
        grouped.setdefault(str(Path(path).parent), []).append(Path(path).name)
    out = ["Interface paths:"]
    for parent, names in sorted(grouped.items()):
        summary = (
            f"{len(names)} event schemas"
            if len(names) > 4 and all(n.endswith(".json") for n in names)
            else ", ".join(names)
        )
        out.append(f"- {parent}/: {summary}")
    return [*out, ""]


def _routes(groups: list[Doc]) -> list[str]:
    rows = sorted(
        (r for group in groups for r in group["routes"]),
        key=lambda r: (r["full_path"], r["method"]),
    )
    if not rows:
        return []
    return [
        "",
        f"## Routes: {len(rows)}",
        *[
            f"- {r['method']} {r['full_path']} → {r['handler'] or '?'}"
            + (
                f" [{r['auth']}]"
                if r.get("auth") not in {None, "authenticated"}
                else ""
            )
            for r in rows
        ],
    ]


def _tables(tables: list[Doc]) -> list[str]:
    return [
        label
        + ": "
        + ", ".join(
            sorted(t["table"] for t in tables if bool(t.get("tenant_scoped")) == scoped)
        )
        for label, scoped in (("Global", False), ("Tenant scoped", True))
        if any(bool(t.get("tenant_scoped")) == scoped for t in tables)
    ]


def _unresolved(items: list[Doc]) -> list[str]:
    return (
        [
            "Unresolved declarations:",
            *[
                "- "
                + item["related_id"].split(":", 1)[-1]
                + " — "
                + item["kind"].replace("_", " ")
                + (" (optional)" if item.get("required") is False else "")
                for item in items
            ],
            "",
        ]
        if items
        else []
    )


def _overview(data: Doc) -> list[str]:
    out: list[str] = []
    if data["events_published"]:
        out += _published(data["events_published"])
    if data["events_consumed"]:
        out += ["", f"## Events consumed: {len(data['events_consumed'])}"]
        out += [f"- {e['channel']} → {e['handler']}" for e in data["events_consumed"]]
    for channel, schema in data.get("event_schemas", {}).items():
        out += ["", "### Event wire schema: " + channel, encode(schema).strip()]
    out += _routes(data["routes"])
    if data["tables"]:
        out += ["", f"## Tables: {len(data['tables'])}"]
        out += _tables(data["tables"])
    if data["sagas"]:
        out += [
            "",
            "Sagas: "
            + ", ".join(f"{s['name']} v{s['version']}" for s in data["sagas"]),
        ]
    if not data["focus"]:
        empty = [
            label
            for label, key in (
                ("provided contracts", "contracts_provided"),
                ("used contracts", "contracts_used"),
                ("published events", "events_published"),
                ("consumed events", "events_consumed"),
                ("routes", "routes"),
                ("tables", "tables"),
                ("sagas", "sagas"),
            )
            if not data[key]
        ]
        if empty:
            out += ["", "None declared: " + ", ".join(empty) + "."]
    if data["infrastructure"]:
        out += ["", "Infrastructure requirements: " + ", ".join(data["infrastructure"])]
    return [*out, ""]


def markdown(envelope: Doc) -> str:
    data, fresh = envelope["data"], envelope["freshness"]
    module = data["module"]
    out = [
        f"# Sandloom context: {module['name']} · {module['source']}",
        "Freshness: " + _FRESHNESS.get(fresh["state"], fresh["state"]),
    ]
    if data["focus"]:
        out.append("Focus: " + data["focus"]["name"])
    if data["rules"]:
        out += ["Rules to read: " + ", ".join(data["rules"])]
    out.append("")
    if data["contracts_provided"] or not data["focus"]:
        out += _contracts("Contracts provided", data["contracts_provided"], True)
    if data["contracts_used"] or not data["focus"]:
        out += _contracts("Contracts used", data["contracts_used"], False)
    out += _overview(data)
    out += _files(data)
    out += _interface_paths(data["interface_files"])
    out += _unresolved(data["unresolved"])
    omissions = [o for o in data["omissions"] if o["reason"] == "outside_focus"]
    if omissions:
        out += [
            "Excluded file metadata: "
            + "; ".join(o["reason"] + ": " + str(o["count"]) for o in omissions),
            "",
        ]
    if "evidence" in fresh:
        out += ["", "Verification evidence: " + encode(fresh["evidence"]).strip()]
    return _text(out)


def render(envelope: Doc, fmt: str) -> str:
    return encode(envelope) if fmt == "json" else markdown(envelope)


def build(
    reader: Reader,
    selector: str,
    *,
    fmt: str,
    require: bool,
    focus_selector: str | None = None,
) -> tuple[Doc, str]:
    module = reader.module(selector)
    if any(v.get("unresolved_bases") for v in module["event_views"]):
        raise InspectionError(
            "incomplete_view",
            "Event view metadata has unresolved bases; declare its wire fields locally",
        )
    before = reader.basis(module)
    owned = reader.owned_paths(module)
    ignored = [p for p in owned if files.ignored(reader.root, p)]
    candidates = [p for p in owned if p not in ignored]
    focused = (
        focus.scope(reader, module, focus_selector, candidates)
        if focus_selector
        else None
    )
    selected = focused["candidates"] if focused else candidates
    whole = not focused or focused["all_interfaces"]
    wires = focused["wires"] if focused else reader.wire_paths(module)
    navigation = inventory.build(reader, module, selected)
    data: Doc = {
        "representation": "module_map",
        "module": {
            k: module[k] for k in ("id", "name", "source", "import_path", "version")
        },
        "focus": {"id": focused["item"]["id"], "name": focused["name"]}
        if focused
        else None,
        "rules": reader.rules(module),
        **interfaces.build(reader, module, focused, wires),
        "routes": interfaces.routes(
            [
                route
                for route in module.get("routes", [])
                if whole
                or (
                    focused is not None
                    and focused["item"]["id"].startswith("route:")
                    and focused["item"]["name"] == f"{route['method']} {route['path']}"
                )
            ]
        ),
        "tables": module.get("models", []) if whole else [],
        "sagas": sorted(
            (
                {"id": p["id"], "name": p["saga_type"], "version": p["version"]}
                for p in reader.doc.get("processes", [])
                if p["owner"] == module["id"]
                and p["id"].startswith("saga:")
                and (
                    whole or (focused is not None and p["id"] == focused["item"]["id"])
                )
            ),
            key=lambda p: p["id"],
        ),
        "infrastructure": sorted(
            e["target"].removeprefix("capability:")
            for e in reader.doc["edges"]
            if whole
            and e["owner"] == module["id"]
            and e["kind"] == "requires_capability"
        ),
        **navigation,
        "interface_files": wires,
        "unresolved": [
            u
            for u in reader.doc["unresolved"]
            if u["module_id"] == module["id"]
            and (
                whole
                or (
                    focused is not None
                    and u.get("related_id") in focused["interface_ids"]
                )
            )
        ],
        "omissions": navigation["omissions"]
        + ([{"reason": "context_ignore", "count": len(ignored)}] if ignored else [])
        + (
            [{"reason": "outside_focus", "count": len(set(candidates) - set(selected))}]
            if set(candidates) - set(selected)
            else []
        ),
    }
    envelope = response(
        "context",
        "context",
        data,
        selection={
            "module_id": module["id"],
            "focus_id": focused["item"]["id"] if focused else None,
        },
        freshness=reader.freshness([module], require=require),
    )
    text = render(envelope, fmt)
    if before != reader.basis(module):
        raise InspectionError(
            "source_changing", "Inputs changed during map capture; retry"
        )
    return envelope, text
