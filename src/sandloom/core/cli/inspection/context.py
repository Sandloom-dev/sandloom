"""Compact module navigation maps; callers open implementation files themselves."""

from __future__ import annotations

from pathlib import Path

from sandloom.core.cli.inspection import files, focus, interfaces, inventory
from sandloom.core.cli.inspection.model import Doc, InspectionError, encode, response
from sandloom.core.cli.inspection.read import Reader


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
            out.append("  - Signature metadata unavailable; read the interface files.")
        peers = ", ".join(p.removeprefix("module:") for p in entry["peers"])
        out.append(
            f"  - {'Callers' if provided else 'Providers'} here: "
            f"{peers or 'none declared'}"
        )
        for dto in entry.get("dtos", []):
            out.append("  - DTO " + dto["ref"])
            out += [
                f"    - {f['name']}: {f['type']}"
                + (" (optional)" if not f["required"] else "")
                for f in dto["fields"]
            ]
    return out + ([""] if entries else ["- None.", ""])


def _files(data: Doc) -> list[str]:
    out, root = ["## Files"], data["module"]["source"] + "/"
    for record in data["files"]:
        symbols = ", ".join(f"{s['name']}:{s['line']}" for s in record["symbols"])
        if record["more_symbols"]:
            symbols += f" +{record['more_symbols']}"
        description = " — ".join(p for p in (record["summary"], symbols) if p)
        description = record.get("parse_error") or description
        out.append(
            f"- {record['path'].removeprefix(root)} · "
            f"{record['bytes'] / 1024:.1f} KiB · {description}"
        )
    for group in data["file_groups"]:
        if group["kind"] == "tests":
            summary = ", ".join(f"{p}: {n}" for p, n in group["directories"].items())
        elif group["kind"] == "migrations":
            summary = f"{group['revision_count']} revisions; declared heads: " + (
                ", ".join(group["heads"]) or "unavailable"
            )
        else:
            summary = ", ".join(group["files"])
        out.append(
            f"- {group['path'].removeprefix(root)}/ · "
            f"{group['count']} files · {summary}"
        )
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
        out.append(f"- {prefix}.{{{', '.join(names)}}}.{version}")
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
            f"{len(names)} event schemas (focus an event for detail)"
            if len(names) > 4 and all(n.endswith(".json") for n in names)
            else ", ".join(names)
        )
        out.append(f"- {parent}/: {summary}")
    return [*out, ""]


def _overview(data: Doc) -> list[str]:
    out: list[str] = []
    if data["events_published"] or not data["focus"]:
        out += _published(data["events_published"])
    if data["events_consumed"] or not data["focus"]:
        out += ["", f"## Events consumed: {len(data['events_consumed'])}"]
        out += [
            f"- {e['channel']} → {e['handler']}" for e in data["events_consumed"]
        ] or ["- None."]
    for channel, schema in data.get("event_schemas", {}).items():
        out += ["", "### Event wire schema: " + channel, encode(schema).strip()]
    if data["routes"] or not data["focus"]:
        out += ["", f"## Routes: {sum(len(g['routes']) for g in data['routes'])}"]
    for group in data["routes"]:
        out.append(
            f"- {group['prefix']} ({len(group['routes'])}): "
            + " · ".join(r["method"] + " " + r["path"] for r in group["routes"])
        )
    if data["tables"] or not data["focus"]:
        out += [
            "",
            f"## Tables: {len(data['tables'])}",
            ", ".join(t["table"] for t in data["tables"]) or "None.",
        ]
    if data["sagas"] or not data["focus"]:
        out += ["Sagas: " + (", ".join(data["sagas"]) or "none"), ""]
    if data["infrastructure"]:
        out += ["Infrastructure requirements: " + ", ".join(data["infrastructure"]), ""]
    return out


def markdown(envelope: Doc) -> str:
    data, fresh = envelope["data"], envelope["freshness"]
    module = data["module"]
    out = [
        f"# Sandloom context: {module['name']} · {module['source']}",
        "Freshness: " + fresh["state"],
        "Module map. Open source files by their paths below.",
    ]
    if fresh["state"] != "verified":
        out.append(
            "Declarations may be stale; run sandloom generate to refresh verification."
        )
    if data["focus"]:
        out.append("Focus: " + data["focus"]["id"])
    out += ["Rules to read: " + ", ".join(data["rules"]), ""]
    if data["contracts_provided"] or not data["focus"]:
        out += _contracts("Contracts provided", data["contracts_provided"], True)
    if data["contracts_used"] or not data["focus"]:
        out += _contracts("Contracts used", data["contracts_used"], False)
    out += _overview(data)
    out += _files(data)
    out += _interface_paths(data["interface_files"])
    if data["unresolved"]:
        out += ["Unresolved declarations: " + encode(data["unresolved"]).strip(), ""]
    if data["omissions"]:
        out += [
            "Excluded file metadata: "
            + "; ".join(
                o["reason"] + ": " + str(o["count"]) for o in data["omissions"]
            ),
            "",
        ]
    out += [
        "## Next",
        f"- Interface detail: sandloom context {module['name']} "
        "--focus CONTRACT_OR_EVENT_ID",
        "- Potential impact: sandloom graph impact CONTRACT_OR_EVENT_ID",
        f"- Check changes: sandloom test {module['name']}",
    ]
    if "evidence" in fresh:
        out += ["", "Verification evidence: " + encode(fresh["evidence"]).strip()]
    return (
        "\n".join("".join(c if c.isprintable() else " " for c in line) for line in out)
        + "\n"
    )


def render(envelope: Doc, fmt: str) -> str:
    return encode(envelope) if fmt == "json" else markdown(envelope)


def _account(envelope: Doc, fmt: str) -> str:
    for _ in range(8):
        text = render(envelope, fmt)
        size = len(text.encode())
        if envelope["data"]["bytes"] == size:
            return text
        envelope["data"]["bytes"] = size
    raise InspectionError("accounting_failed", "Cannot stabilize map byte accounting")


def build(
    reader: Reader,
    selector: str,
    *,
    budget: int,
    fmt: str,
    detail: str,
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
    data: Doc = {
        "representation": "module_map",
        "module": {
            k: module[k] for k in ("id", "name", "source", "import_path", "version")
        },
        "focus": {"id": focused["item"]["id"], "limits": focused["limits"]}
        if focused
        else None,
        "rules": reader.rules(module),
        **interfaces.build(reader, module, focused),
        "routes": interfaces.routes(module.get("routes", [])) if whole else [],
        "tables": module.get("models", []) if whole else [],
        "sagas": sorted(
            p["id"]
            for p in reader.doc.get("processes", [])
            if p["owner"] == module["id"]
            and p["id"].startswith("saga:")
            and (whole or (focused is not None and p["id"] == focused["item"]["id"]))
        ),
        "infrastructure": sorted(
            e["target"].removeprefix("capability:")
            for e in reader.doc["edges"]
            if e["owner"] == module["id"] and e["kind"] == "requires_capability"
        ),
        **inventory.build(reader, module, selected),
        "interface_files": focused["wires"] if focused else reader.wire_paths(module),
        "unresolved": [
            u for u in reader.doc["unresolved"] if u["module_id"] == module["id"]
        ],
        "budget": budget,
        "bytes": 0,
        "omissions": (
            [{"reason": "context_ignore", "count": len(ignored)}] if ignored else []
        )
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
        freshness=reader.freshness([module], require=require, full=detail == "full"),
    )
    text = _account(envelope, fmt)
    if len(text.encode()) > budget:
        raise InspectionError(
            "budget_exceeded",
            f"Complete module map needs {len(text.encode())} bytes; "
            f"focus the map or raise --max-bytes (budget {budget})",
        )
    if before != reader.basis(module):
        raise InspectionError(
            "source_changing", "Inputs changed during map capture; retry"
        )
    return envelope, text
