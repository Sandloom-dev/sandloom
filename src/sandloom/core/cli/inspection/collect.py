"""Trusted authoring collector. Only generation imports this module."""

from __future__ import annotations

import ast
import importlib.metadata
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import quote

import sandloom.core.contract_compat as _contract_compat
from sandloom.core.archcheck.api10_rules import event_classes
from sandloom.core.cli.inspection import files, processes
from sandloom.core.cli.inspection.model import Doc, InspectionError, digest, encode, sha
from sandloom.core.descriptors.module_names import module_name


def layout(rules: Any) -> Doc:
    root = rules.root.resolve()
    return {
        "source_root": rules.source_root.resolve().relative_to(root).as_posix(),
        "modules_path": rules.modules_path.resolve().relative_to(root).as_posix(),
        "modules_package": rules.modules_package,
        "shared_dir": rules.shared_dir.resolve().relative_to(root).as_posix(),
        "shared_package": rules.shared_dto_package,
        "policy_sha256": sha((root / "architecture_rules.yml").read_bytes()),
        "role_contract": "src/sandloom/AGENTS.md"
        if rules.is_framework_tree
        else "AGENTS.md",
    }


def capture(rules: Any) -> dict[str, str]:
    "Membership and content before composition, repeated before publishing."
    root = rules.root.resolve()
    paths = ["architecture_rules.yml"]
    paths += [
        p
        for p in (
            "pyproject.toml",
            "uv.lock",
            "AGENTS.md",
            "CLAUDE.md",
            ".sandloomignore",
        )
        if (root / p).is_file()
    ]
    for name in rules.business_modules():
        directory = (rules.modules_path / name).relative_to(root).as_posix()
        paths += files.candidates(root, directory)
        paths += files.instructions(root, directory, layout(rules)["role_contract"])
    return files.fingerprints(root, paths)


def _node(nodes: dict[str, Doc], kind: str, name: str, **attrs: Any) -> str:
    identity = f"{kind}:{quote(name, safe='._-')}"
    previous = nodes.setdefault(identity, {"id": identity, "kind": kind, "name": name})
    if (
        kind == "module"
        and "import_path" in attrs
        and previous.get("import_path", attrs["import_path"]) != attrs["import_path"]
    ):
        raise ValueError("Conflicting module identities")
    previous.update(attrs)
    return identity


def _edge(
    edges: list[Doc], kind: str, source: str, target: str, owner: str, **attrs: Any
) -> None:
    edges.append(
        {
            "id": f"edge:{digest([kind, source, target])}",
            "kind": kind,
            "source": source,
            "target": target,
            "owner": owner,
            "evidence_kind": "registered_declaration",
            **attrs,
        }
    )


def _source(entry: Any, root: Path, source_root: Path) -> str | None:
    imported = sys.modules.get(entry.name)
    path = getattr(imported, "__file__", None)
    if path and Path(path).resolve().parent.is_relative_to(source_root.resolve()):
        return Path(path).resolve().parent.relative_to(root).as_posix()
    return None


def _wires(entry: Any, shared: str, planned: dict[str, str]) -> list[str]:
    names = {c.name for c in (*entry.contracts_provided, *entry.contracts_consumed)}
    result = [
        f"{shared}/contracts/{name.replace('.', '_').replace('-', '_')}.py"
        for name in sorted(names)
        if f"{shared}/contracts/{name.replace('.', '_').replace('-', '_')}.py"
        in planned
    ]
    result += [
        f"{shared}/events/{e.channel}.json"
        for e in entry.events
        if e.role == "producer"
    ]
    result += [
        f"{shared}/events/{c.channel}.json"
        for c in entry.consumers
        if f"{shared}/events/{c.channel}.json" in planned
    ]
    return sorted(set(result))


def _views(root: Path, directory: str | None, name: str) -> list[Doc]:
    result: list[Doc] = []
    if directory:
        for path in files.candidates(root, directory):
            if not path.endswith(".py") or "tests" in Path(path).parts:
                continue
            try:
                tree = ast.parse(files.source(root, path))
            except (UnicodeError, SyntaxError):
                continue
            except InspectionError as exc:
                if exc.kind != "file_too_large":
                    raise
                continue
            declared = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
            result.extend(
                {
                    "channel": v.channel,
                    "path": path,
                    "symbol": v.node.name,
                    "fields": v.fields,
                    "optional": sorted(v.optional),
                    "unresolved_bases": [
                        ast.unparse(b)
                        for b in v.node.bases
                        if not isinstance(b, ast.Name)
                        or b.id not in declared | {"Event"}
                    ],
                }
                for v in event_classes(root / path)
                if v.role == "view"
                or (v.role == "auto" and not v.channel.startswith(name + "."))
            )
    return result


def _registrations(
    entry: Any, descriptor: Any, card: Doc, nodes: dict[str, Doc], edges: list[Doc]
) -> None:
    owner = card["id"]
    for contract in entry.contracts_provided:
        target = _node(nodes, "contract", contract.name)
        _edge(
            edges,
            "provides_contract",
            owner,
            target,
            owner,
            methods=list(contract.methods),
        )
    reasons = {c.name: c.reason for c in descriptor.consumes} if descriptor else {}
    for use in entry.contracts_consumed:
        target = _node(nodes, "contract", use.name)
        _edge(
            edges,
            "requires_contract",
            owner,
            target,
            owner,
            required=use.required,
            methods=list(use.methods),
            reason=reasons.get(use.name, ""),
        )
    for event in entry.events:
        if event.role == "producer":
            target = _node(nodes, "event", event.channel)
            _edge(edges, "produces_event", owner, target, owner)
    for consumer in entry.consumers:
        event_id = _node(nodes, "event", consumer.channel)
        target = _node(
            nodes,
            "consumer",
            f"{card['name']}.{consumer.name}",
            **{k: v for k, v in asdict(consumer).items() if k != "name"},
        )
        _edge(edges, "owns_consumer", owner, target, owner)
        _edge(edges, "consumes_event", event_id, target, owner)
    for projection in entry.projections:
        target = _node(nodes, "projection", f"{card['name']}.{projection.name}")
        _edge(edges, "owns_projection", owner, target, owner)
        for channel in projection.source_channels:
            event_id = _node(nodes, "event", channel)
            _edge(edges, "projects_event", event_id, target, owner)


def _requirements(
    descriptor: Any, owner: str, nodes: dict[str, Doc], edges: list[Doc]
) -> None:
    if descriptor:
        for capability in descriptor.capabilities:
            cap = asdict(capability)
            cap["features"] = sorted(cap["features"])
            target = _node(
                nodes,
                "capability",
                f"{capability.capability}.{capability.instance}.{capability.version}",
            )
            _edge(edges, "requires_capability", owner, target, owner, **cap)
        for dependency in descriptor.depends_on:
            target = _node(nodes, "module", module_name(dependency))
            _edge(edges, "declares_ordering", owner, target, owner)


def collect(app: Any, rules: Any, generated: list[Any]) -> Doc:
    root = rules.root.resolve()
    config = layout(rules)
    planned = {
        p.path.resolve().relative_to(root).as_posix(): p.content
        for p in generated
        if p.path.resolve().is_relative_to(root)
    }
    route_documents = {
        value["module"]: value["routes"]
        for path, text in planned.items()
        if path.endswith("/routes.snapshot.json")
        for value in [json.loads(text)]
    }
    route_sources = processes.route_sources(app, root)
    descriptors = {d.name: d for d in app.state.core.module_descriptors}
    distributions = {
        ep.value: {ep.dist.metadata["Name"]: ep.dist.version}
        for ep in importlib.metadata.entry_points(group="sandloom.modules")
        if ep.dist is not None
    }
    nodes: dict[str, Doc] = {}
    edges: list[Doc] = []
    modules = []
    wires: dict[str, Doc] = {}
    for entry in sorted(app.state.app_manifest.modules, key=lambda m: m.name):
        owner = _node(nodes, "module", module_name(entry.name), import_path=entry.name)
        directory = _source(entry, root, rules.source_root)
        nodes[owner]["source"] = directory
        card = {
            "id": owner,
            "name": module_name(entry.name),
            "import_path": entry.name,
            "version": entry.version,
            "optional": entry.optional,
            "distributions": distributions.get(entry.name, {}),
            "source": directory,
            "instructions": files.instructions(root, directory, config["role_contract"])
            if directory
            else [],
            "models": [asdict(m) for m in entry.models],
            "routes": [asdict(r) for r in entry.routes],
            "deviations": [asdict(d) for d in entry.deviations],
            "chain": {"database": entry.chain.database, "chain": entry.chain.chain}
            if entry.chain
            else None,
            "sagas": [asdict(s) for s in entry.sagas],
            "wire_paths": _wires(entry, config["shared_dir"], planned),
            "event_views": [],
        }
        policies = {
            (r["method"], r["path"]): r for r in route_documents.get(entry.name, [])
        }
        card["routes"] = [
            {
                **r,
                **policies.get((r["method"], r["path"]), {}),
                "source": route_sources.get((entry.name, r["method"], r["path"])),
            }
            for r in card["routes"]
        ]
        card["event_views"] = _views(root, directory, card["name"])
        modules.append(card)
        descriptor = descriptors.get(entry.name)
        _registrations(entry, descriptor, card, nodes, edges)
        _requirements(descriptor, owner, nodes, edges)
    for path, text in sorted(planned.items()):
        if path.startswith(config["shared_dir"] + "/"):
            wires[path] = {"sha256": sha(text.encode()), "kind": "generated_wire"}
    provided = {e["target"] for e in edges if e["kind"] == "provides_contract"}
    unresolved = [
        {
            "kind": "provider_not_in_composition",
            "module_id": e["owner"],
            "related_id": e["target"],
            "required": e["required"],
        }
        for e in edges
        if e["kind"] == "requires_contract" and e["target"] not in provided
    ]
    return {
        "schema": "sandloom.inspection",
        "schema_version": 1,
        "producer_protocol": 1,
        "layout": config,
        "scope": {
            "kind": "authoring_composition",
            "module_ids": sorted(m["id"] for m in modules),
        },
        "modules": modules,
        "nodes": sorted(nodes.values(), key=lambda n: n["id"]),
        "edges": sorted(edges, key=lambda e: e["id"]),
        "wire_documents": wires,
        "source_locators": {
            m["id"]: {
                "kind": "registered_declaration",
                "source_root": m["source"],
                "distributions": m["distributions"],
            }
            for m in modules
        },
        "event_schemas": {
            e.channel: e.schema for e in app.state.core.list_event_schemas() if e.schema
        },
        "unresolved": unresolved,
        "processes": processes.collect(app.state.core, root, modules),
        "contract_descriptions": {
            name: value
            for snapshot in _contract_compat.module_snapshots(app.state.core).values()
            for name, value in snapshot["contracts"].items()
        },
    }


def rendered(document: Doc) -> str:
    return encode(document)
