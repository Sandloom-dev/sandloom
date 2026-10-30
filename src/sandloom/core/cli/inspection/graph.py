"Witnessed module projections, typed graph exports and safe presentation."

from __future__ import annotations

import html
from collections import defaultdict
from pathlib import Path

from sandloom.core.cli.inspection.model import Doc, digest, encode, response
from sandloom.core.cli.inspection.read import Reader


def relations(document: Doc) -> list[Doc]:
    edges = document["edges"]
    providers = {e["target"]: e for e in edges if e["kind"] == "provides_contract"}
    owners = {
        e["target"]: e
        for e in edges
        if e["kind"] in {"owns_consumer", "owns_projection"}
    }
    producers = {e["target"]: e for e in edges if e["kind"] == "produces_event"}
    result = []
    for edge in edges:
        witness = []
        source, target = edge["source"], edge["target"]
        kind = edge["kind"]
        if kind == "requires_contract" and target in providers:
            provider = providers[target]
            target = provider["source"]
            witness = [edge["id"], provider["id"]]
            kind = "required_contract" if edge["required"] else "optional_contract"
        elif (
            kind in {"consumes_event", "projects_event"}
            and source in producers
            and target in owners
        ):
            producer, owner = producers[source], owners[target]
            source, target = producer["source"], owner["source"]
            witness = [producer["id"], edge["id"], owner["id"]]
            kind = "event_impact"
        elif kind == "declares_ordering":
            witness = [edge["id"]]
            kind = "ordering"
        if witness:
            result.append(
                {
                    "id": "relation:" + digest([kind, source, target, witness]),
                    "kind": kind,
                    "source": source,
                    "target": target,
                    "witnesses": witness,
                }
            )
    return sorted(result, key=lambda r: r["id"])


def cycles(links: list[Doc]) -> list[Doc]:
    """SCCs per relation family, including intentional event feedback."""
    result = []
    for kind in sorted({r["kind"] for r in links}):
        adjacent: dict[str, set[str]] = defaultdict(set)
        for link in links:
            if link["kind"] == kind:
                adjacent[link["source"]].add(link["target"])
                adjacent[link["target"]]
        # Reachability intersection avoids recursion limits on a large declared map.
        pending = set(adjacent)
        while pending:
            start = min(pending)
            reached = _reachable(start, adjacent)
            component = sorted(n for n in reached if start in _reachable(n, adjacent))
            pending.difference_update(component)
            if len(component) > 1 or start in adjacent[start]:
                result.append(
                    {
                        "kind": kind,
                        "module_ids": component,
                        "meaning": "declared cycle; not proof of deadlock",
                    }
                )
    return result


def _reachable(start: str, adjacent: dict[str, set[str]]) -> set[str]:
    seen = {start}
    pending = [start]
    while pending:
        for target in adjacent[pending.pop()]:
            if target not in seen:
                seen.add(target)
                pending.append(target)
    return seen


def build(
    reader: Reader,
    *,
    selector: str | None,
    hops: int,
    discovery: bool,
    detail: str,
    require: bool,
) -> Doc:
    links = relations(reader.doc)
    selected = (
        {reader.module(selector)["id"]}
        if selector
        else {m["id"] for m in reader.modules}
    )
    for _ in range(hops if selector else 0):
        selected |= {
            end
            for link in links
            if link["source"] in selected or link["target"] in selected
            for end in (link["source"], link["target"])
        }
    modules = [m for m in reader.modules if m["id"] in selected]
    fresh = reader.freshness(
        modules, require=require, full=detail == "full", tolerate_missing=True
    )
    if discovery:
        data = {
            "relations_included": False,
            "modules": [
                {
                    "id": m["id"],
                    "import_path": m["import_path"],
                    "source_available": m["source"] is not None,
                    "freshness": fresh["modules"][m["id"]],
                }
                for m in modules
            ],
            "counts": {
                "modules": len(modules),
                "nodes": len(reader.doc["nodes"]),
                "edges": len(reader.doc["edges"]),
            },
        }
        return response("graph", "modules", data, freshness=fresh)
    edges = [e.copy() for e in reader.doc["edges"] if e["owner"] in selected]
    node_ids = selected | {end for e in edges for end in (e["source"], e["target"])}
    nodes = [n for n in reader.doc["nodes"] if n["id"] in node_ids]
    projected = [
        r for r in links if r["source"] in selected and r["target"] in selected
    ]
    if detail == "compact":
        edges = [
            {k: v for k, v in e.items() if k not in {"reason", "evidence_kind"}}
            for e in edges
        ]
    data = {
        "scope": reader.doc["scope"],
        "evidence": {
            k: v
            for k, v in reader.doc.get("source_locators", {}).items()
            if k in selected
        },
        "projection": "incoming_and_outgoing_contracts_and_events",
        "nodes": nodes,
        "edges": edges,
        "module_relations": projected,
        "cycles": cycles(projected),
        "unresolved": [
            u for u in reader.doc["unresolved"] if u["module_id"] in selected
        ],
        "hidden": {
            "nodes": len(reader.doc["nodes"]) - len(nodes),
            "edges": len(reader.doc["edges"]) - len(edges),
        },
    }
    return response(
        "graph",
        "graph",
        data,
        selection={"module_ids": sorted(selected), "hops": hops if selector else None},
        freshness=fresh,
    )


def mermaid(envelope: Doc) -> str:
    data = envelope["data"]
    names = {n["id"]: f"n{i}" for i, n in enumerate(data["nodes"])}
    out = [
        "flowchart LR",
        f"%% Declared topology; freshness: {envelope['freshness']['state']}",
    ]
    for node in data["nodes"]:
        label = (
            html.escape(node["name"], quote=True)
            .replace("\n", " ")
            .replace("`", "&#96;")
        )
        out.append(f'  {names[node["id"]]}["{label}"]')
    out.extend(
        f"  {names[e['source']]} -->|{e['kind']}| {names[e['target']]}"
        for e in data["edges"]
    )
    return "\n".join(out) + "\n"


def viewer(envelope: Doc) -> str:
    payload = (
        encode(envelope)
        .replace("<", "\\u003c")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    template = Path(__file__).with_name("viewer.html").read_text(encoding="utf-8")
    return template.replace("__SANDLOOM_GRAPH_JSON__", payload)
