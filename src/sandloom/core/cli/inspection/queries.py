"""Bounded declared impact and semantic comparisons over saved inspection data."""

from __future__ import annotations

from collections import deque
from pathlib import Path

from sandloom.core.cli.inspection import files, graph
from sandloom.core.cli.inspection.model import Doc, InspectionError, digest, response
from sandloom.core.cli.inspection.read import Reader, parse
from sandloom.core.contracts.compatibility import compare_contract
from sandloom.core.contracts.schema_compatibility import schema_problems


def item(reader: Reader, selector: str) -> Doc:
    matches: list[Doc] = [
        n
        for n in [*reader.doc["nodes"], *reader.doc.get("processes", [])]
        if selector in (n["id"], n.get("name"), n.get("saga_type"))
    ]
    if len(matches) != 1:
        raise InspectionError(
            "unknown_item" if not matches else "ambiguous_item",
            "Choose one stable ID from graph JSON",
        )
    return matches[0]


def owners(reader: Reader, selected: Doc) -> set[str]:
    if selected["id"].startswith("module:"):
        return {selected["id"]}
    if selected.get("owner"):
        return {selected["owner"]}
    return {
        e["owner"]
        for e in reader.doc["edges"]
        if selected["id"] in (e["source"], e["target"])
    }


def impact(reader: Reader, selector: str, require: bool) -> Doc:
    selected = item(reader, selector)
    links = graph.relations(reader.doc)
    # Caller -> provider declarations have the reverse potential-change direction.
    adjacent: dict[str, list[Doc]] = {}
    for link in links:
        if link["kind"] == "ordering":
            continue
        origin, target = link["source"], link["target"]
        if link["kind"] in {"required_contract", "optional_contract"}:
            origin, target = target, origin
        adjacent.setdefault(origin, []).append({"target": target, "relation": link})
    seeds = owners(reader, selected)
    direct = {
        s: [
            e
            for e in reader.doc["edges"]
            if e["owner"] == s and selected["id"] in (e["source"], e["target"])
        ]
        for s in seeds
    }
    origins = {s: s for s in seeds}
    paths: dict[str, list[Doc]] = {s: [] for s in seeds}
    pending = deque(sorted(seeds))
    while pending:
        source = pending.popleft()
        for link in sorted(adjacent.get(source, []), key=lambda r: r["target"]):
            if link["target"] not in paths:
                paths[link["target"]] = paths[source] + [link["relation"]]
                origins[link["target"]] = origins[source]
                pending.append(link["target"])
    affected = [m for m in reader.modules if m["id"] in paths]
    return response(
        "graph",
        "impact",
        {
            "item": selected,
            "meaning": (
                "Potential impact: direct interface users plus transitive module "
                "reachability, including other interfaces; an overapproximation, "
                "not complete call-site or test coverage"
            ),
            "affected": [
                {
                    "module_id": m["id"],
                    "witness_path": paths[m["id"]],
                    "seed_module_id": origins[m["id"]],
                    "seed_witnesses": direct[origins[m["id"]]],
                    "scope": "transitive_module_reachability"
                    if paths[m["id"]]
                    else "direct_interface_or_owner",
                    "test_argv": ["sandloom", "test", m["name"]],
                }
                for m in affected
            ],
            "limits": [
                (
                    "A module may use an interface only conditionally; optional "
                    "dependencies remain optional."
                ),
                "Run normal quality gates; this report cannot justify skipping tests.",
            ],
        },
        selection={"item_id": selected["id"]},
        freshness=reader.freshness(affected, require=require, tolerate_missing=True),
    )


def _entities(document: Doc) -> dict[str, Doc]:
    result = {n["id"]: n for n in document["nodes"]}
    for edge in document["edges"]:
        result[edge["id"]] = edge
    for module in document["modules"]:
        result[module["id"]] = {
            k: v
            for k, v in module.items()
            if k not in {"instructions", "wire_paths", "event_views"}
        }
        result[module["id"]]["event_views"] = [
            {k: v for k, v in view.items() if k != "path"}
            for view in module.get("event_views", [])
        ]
    for process in document.get("processes", []):
        result[process["id"]] = {
            **process,
            **(
                {
                    "steps": [
                        {
                            k: v
                            for k, v in step.items()
                            if k not in {"source", "compensation_source"}
                        }
                        for step in process["steps"]
                    ]
                }
                if "steps" in process
                else {}
            ),
        }
    for name, value in document.get("contract_descriptions", {}).items():
        key = "contract:" + name
        result[key] = {**result.get(key, {"id": key, "name": name}), "interface": value}
    for name, value in document.get("event_schemas", {}).items():
        key = "event:" + name
        result[key] = {**result.get(key, {"id": key, "name": name}), "interface": value}
    return result


def compare(reader: Reader, before: str, require: bool) -> Doc:
    # Historical indexes are data: never resolve their source paths.
    previous = parse(files.read(reader.root, before))
    if (
        previous.get("schema") != "sandloom.inspection"
        or previous.get("schema_version") != 1
    ):
        raise InspectionError(
            "unsupported_schema",
            "Comparison requires a saved inspection index, schema 1",
        )
    old, new = _entities(previous), _entities(reader.doc)
    changes = []
    for identity in sorted(old.keys() | new.keys()):
        left, right = old.get(identity), new.get(identity)
        if left == right:
            continue
        kind = "added" if left is None else "removed" if right is None else "changed"
        compatibility: Doc = {"state": "not_assessed", "reasons": []}
        if identity.startswith(("contract:", "event:")):
            a, b = (left or {}).get("interface"), (right or {}).get("interface")
            if identity.startswith("contract:") and a and b:
                report = compare_contract(identity, a, b)
                compatibility = {
                    "state": "breaking"
                    if report.errors
                    else "unknown"
                    if report.warnings
                    else "compatible",
                    "reasons": report.errors + report.warnings,
                }
            elif identity.startswith("event:") and a and b:
                problems = schema_problems(a, b)
                compatibility = {
                    "state": "breaking" if problems else "compatible",
                    "reasons": problems,
                }
            else:
                compatibility = {
                    "state": "unknown",
                    "reasons": [
                        (
                            "Interface unavailable on one side; "
                            "addition/removal requires review"
                        )
                    ],
                }
        changes.append(
            {
                "id": identity,
                "change": kind,
                "before": left,
                "after": right,
                "compatibility": compatibility,
            }
        )
    return response(
        "graph",
        "diff",
        {
            "basis": {
                "before": Path(before).as_posix(),
                "before_digest": digest(previous),
                "after_digest": digest(reader.doc),
            },
            "meaning": (
                "Declared semantic changes; implementation bodies and runtime "
                "behavior are not compared"
            ),
            "counts": {
                kind: sum(c["change"] == kind for c in changes)
                for kind in ("added", "removed", "changed")
            },
            "changes": changes,
        },
        freshness=reader.freshness(
            reader.modules, require=require, tolerate_missing=True
        ),
    )
