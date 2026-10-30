"Witnessed module projections, typed graph exports and safe presentation."

from __future__ import annotations

import html
from pathlib import Path

from sandloom.core.cli.inspection import sequences
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


def _postorder(adjacent: dict[str, set[str]]) -> list[str]:
    seen: set[str] = set()
    finished: list[str] = []
    for start in adjacent:
        pending = [(start, False)]
        while pending:
            node, exiting = pending.pop()
            if exiting:
                finished.append(node)
            elif node not in seen:
                seen.add(node)
                pending.append((node, True))
                pending.extend(
                    (target, False) for target in adjacent[node] if target not in seen
                )
    return finished


def _components(adjacent: dict[str, set[str]]) -> list[list[str]]:
    """Iterative Kosaraju: O(V+E) traversal, deterministic component output."""
    reverse: dict[str, set[str]] = {n: set() for n in adjacent}
    for node, targets in adjacent.items():
        for target in targets:
            reverse[target].add(node)
    finished = _postorder(adjacent)
    seen: set[str] = set()
    groups = []
    for start in reversed(finished):
        if start in seen:
            continue
        group, todo = [], [start]
        seen.add(start)
        while todo:
            node = todo.pop()
            group.append(node)
            for target in reverse[node]:
                if target not in seen:
                    seen.add(target)
                    todo.append(target)
        groups.append(sorted(group))
    return sorted(groups)


def cycles(links: list[Doc]) -> list[Doc]:
    """SCCs per relation family, including intentional event feedback."""
    families: dict[str, dict[str, set[str]]] = {}
    for link in links:
        adjacent = families.setdefault(link["kind"], {})
        adjacent.setdefault(link["source"], set()).add(link["target"])
        adjacent.setdefault(link["target"], set())
    return [
        {
            "kind": kind,
            "module_ids": group,
            "meaning": "declared cycle; not proof of deadlock",
        }
        for kind, adjacent in sorted(families.items())
        for group in _components(adjacent)
        if len(group) > 1 or group[0] in adjacent[group[0]]
    ]


def build(
    reader: Reader,
    *,
    selector: str | None,
    hops: int,
    discovery: bool,
    detail: str,
    require: bool,
    direction: str = "both",
    module_ids: set[str] | None = None,
) -> Doc:
    links = relations(reader.doc)
    selected = (
        {reader.module(selector)["id"]}
        if selector
        else {m["id"] for m in reader.modules}
    )
    if module_ids is not None:
        selected = set(module_ids)
    for _ in range(hops if selector and module_ids is None else 0):
        selected |= {
            end
            for link in links
            if (direction in {"both", "outgoing"} and link["source"] in selected)
            or (direction in {"both", "incoming"} and link["target"] in selected)
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
        "scope": {**reader.doc["scope"], "module_ids": sorted(selected)},
        "evidence": {
            k: v
            for k, v in reader.doc.get("source_locators", {}).items()
            if k in selected
        },
        "projection": direction + "_contracts_and_events",
        "modules": modules
        if detail == "full"
        else [
            {
                **{
                    k: m[k]
                    for k in ("id", "name", "import_path", "source", "version", "chain")
                    if k in m
                },
                "model_count": len(m.get("models", [])),
                "route_count": len(m.get("routes", [])),
            }
            for m in modules
        ],
        "process_counts": {
            kind: sum(
                p["owner"] in selected and p["id"].startswith(kind + ":")
                for p in reader.doc.get("processes", [])
            )
            for kind in ("saga", "route")
        },
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
    if detail == "full":
        data["processes"] = [
            p for p in reader.doc.get("processes", []) if p["owner"] in selected
        ]
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


def viewer(envelope: Doc, reader: Reader | None = None) -> str:
    workspace = envelope
    if reader is not None:
        selected = set(envelope["selection"]["module_ids"])
        process_ids = {p["id"] for p in envelope["data"].get("processes", [])}
        topology = (
            envelope
            if envelope["kind"] == "graph"
            else build(
                reader,
                selector=None,
                hops=1,
                discovery=False,
                detail="full",
                require=False,
                module_ids=selected,
            )
        )
        workspace = {
            **envelope,
            "workspace": topology["data"],
            "saga_states": sequences.states(),
            "sequences": [
                sequences.project(p)
                for p in reader.doc.get("processes", [])
                if p["owner"] in selected
                and (
                    envelope["kind"] not in {"sequence", "state"}
                    or p["id"] in process_ids
                )
            ],
        }
    payload = (
        encode(workspace)
        .replace("<", "\\u003c")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    template = Path(__file__).with_name("viewer.html").read_text(encoding="utf-8")
    return (
        template.replace(
            "__SANDLOOM_VIEWER_CSS__",
            Path(__file__).with_name("viewer.css").read_text(encoding="utf-8"),
        )
        .replace(
            "__SANDLOOM_VIEWER_JS__",
            Path(__file__).with_name("viewer.js").read_text(encoding="utf-8"),
        )
        .replace("__SANDLOOM_GRAPH_JSON__", payload)
    )
