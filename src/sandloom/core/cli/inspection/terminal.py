"""Plain, deterministic terminal summaries over the same inspection envelope."""

from __future__ import annotations

from sandloom.core.cli.inspection.model import Doc
from sandloom.core.cli.inspection.text import (
    count_label,
    docstring_text,
    freshness_label,
)

HEADER = "Sandloom architecture · declared model"


def _safe(value: object) -> str:
    return "".join(
        c if c.isprintable() else c.encode("unicode_escape").decode("ascii")
        for c in str(value)
    )


def _tree(label: str, children: list[str]) -> list[str]:
    return [
        label,
        *[
            ("└─ " if i == len(children) - 1 else "├─ ") + child
            for i, child in enumerate(children)
        ],
    ]


def _topology(data: Doc) -> list[str]:
    nodes = {n["id"]: n.get("name", n["id"]) for n in data.get("nodes", [])}
    lines = []
    for module in data["modules"]:
        relations: dict[str, list[str]] = {}
        for edge in data.get("edges", []):
            if edge["owner"] != module["id"] or edge["kind"] in {
                "owns_consumer",
                "owns_projection",
            }:
                continue
            source = nodes.get(edge["source"], edge["source"])
            target = nodes.get(edge["target"], edge["target"])
            label = (
                target if edge["source"] == module["id"] else source + " → " + target
            )
            if edge.get("required") is False:
                label += " [optional]"
            relations.setdefault(edge["kind"].replace("_", " "), []).append(label)
        children = [
            kind + ": " + "; ".join(sorted(labels))
            for kind, labels in sorted(relations.items())
        ]
        lines += _tree(module.get("name", module["id"]), children)
    for cycle in data.get("cycles", []):
        lines.append(f"Cycle ({cycle['kind']}): " + ", ".join(cycle["module_ids"]))
    for fact in data.get("unresolved", []):
        lines.append(
            "Unresolved: "
            + fact["module_id"].removeprefix("module:")
            + " needs "
            + fact["related_id"]
            + (" (required)" if fact["required"] else " (optional)")
            + ", not in this composition"
        )
    return lines or ["No modules in this scope."]


def _overview(data: Doc) -> list[str]:
    nodes = {n["id"]: n.get("name", n["id"]) for n in data["nodes"]}
    relations = data["module_relations"]
    lines = []
    for module in data["modules"]:
        owner = module["id"]
        edges = [e for e in data["edges"] if e["owner"] == owner]
        facts = [
            count_label(
                module.get("route_count", len(module.get("routes", []))), "route"
            ),
            count_label(
                module.get("model_count", len(module.get("models", []))), "table"
            ),
        ]
        provides = sorted(
            {nodes[e["target"]] for e in edges if e["kind"] == "provides_contract"}
        )
        calls = sorted(
            {
                nodes[r["target"]]
                for r in relations
                if r["source"] == owner
                and r["kind"] in {"required_contract", "optional_contract"}
            }
        )
        listens = sorted(
            {
                nodes[r["source"]]
                for r in relations
                if r["target"] == owner and r["kind"] == "event_impact"
            }
        )
        events = len({e["target"] for e in edges if e["kind"] == "produces_event"})
        if provides:
            facts.append("provides " + ", ".join(provides))
        if events:
            facts.append(count_label(events, "event"))
        if calls:
            facts.append("calls " + ", ".join(calls))
        if listens:
            facts.append("listens to " + ", ".join(listens))
        missing = sum(u["module_id"] == owner for u in data["unresolved"])
        if missing:
            facts.append(f"{missing} unresolved")
        lines += [
            module["name"] + ": " + docstring_text(module.get("summary", "")),
            "└─ " + " · ".join(facts),
        ]
    return lines or ["No modules in this scope."]


def _depth_label(module: Doc) -> str:
    if module["in_cycle"]:
        return "cycle"
    return (
        f"{module['depth']} + cycle"
        if module["reaches_cycle"]
        else str(module["depth"])
    )


def _risks(data: Doc) -> list[str]:
    """Synchronous contract coupling as a table, riskiest modules first."""
    coupling = data["sync_coupling"]
    names = {n["id"]: n.get("name", n["id"]) for n in data["nodes"]}

    def label(module_id: str) -> str:
        return _safe(names.get(module_id, module_id.removeprefix("module:")))

    ordered = sorted(
        coupling["modules"],
        key=lambda m: (
            not m["in_cycle"],
            -(m["depth"] or 0),
            -m["fan_in"],
            label(m["module_id"]),
        ),
    )
    rows = [("Module", "Synchronous dependencies", "Depth", "Fan-in")]
    for module in ordered:
        depends = ", ".join(
            label(d["module_id"]) + ("" if d["required"] else " (optional)")
            for d in module["depends_on"]
        )
        rows.append(
            (
                label(module["module_id"]),
                depends or "-",
                _depth_label(module),
                str(module["fan_in"]),
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    lines = [
        "Synchronous coupling: " + coupling["meaning"],
        *(
            "  ".join(row[i].ljust(widths[i]) for i in range(4)).rstrip()
            for row in rows
        ),
    ]
    deepest = next(
        (
            m
            for m in ordered
            if not m["in_cycle"] and m["depth"] == coupling["max_depth"]
        ),
        None,
    )
    if deepest and coupling["max_depth"] > 0:
        lines.append(
            f"Longest chain (depth {coupling['max_depth']}): "
            + " → ".join(label(i) for i in deepest["longest_chain"])
        )
    if coupling["critical"]:
        lines.append(
            "Most depended on: "
            + ", ".join(
                f"{label(c['module_id'])} (fan-in {c['fan_in']})"
                for c in coupling["critical"]
            )
        )
    return lines


def _messages(title: str, messages: list[Doc]) -> list[str]:
    lines = [title]
    for message in messages:
        lines += _tree(
            f"{message['source']} → {message['target']}: {message['label']}",
            [
                str(message[k])
                for k in ("kind", "transaction", "condition")
                if message.get(k)
            ],
        )
    return lines


def render(envelope: Doc, *, overview: bool = False, risks: bool = False) -> str:
    data, kind = envelope["data"], envelope["kind"]
    lines = [
        HEADER,
        "Freshness: " + freshness_label(envelope["freshness"]["state"]),
        "",
    ]
    if kind in {"graph", "modules"}:
        lines += _overview(data) if kind == "graph" and overview else _topology(data)
        if kind == "graph" and risks:
            lines += ["", *_risks(data)]
    elif kind == "impact":
        lines += [
            "Potential impact: " + data["item"]["id"],
            data["meaning"].removeprefix("Potential impact: "),
        ]
        lines += [
            f"{'↳' if item['witness_path'] else '•'} {item['module_id']} "
            f"[{item['scope']}] · " + " ".join(item["test_argv"])
            for item in data["affected"]
        ]
    elif kind == "diff":
        lines += ["Changes vs " + data["basis"]["before"], data["meaning"]]
        lines.append(" · ".join(f"{n} {k}" for k, n in data["counts"].items()))
        for item in data["changes"]:
            lines += _tree(
                f"{item['change']}: {item['id']} [{item['compatibility']['state']}]",
                item["compatibility"]["reasons"],
            )
    else:
        process = data.get("process")
        if process:
            lines += [process["name"], process["evidence"]]
            if kind == "state":
                lines += _messages("Conditional states", data["state"]["transitions"])
            else:
                lines += _messages("Forward path", process["steps"])
                if process["alternatives"]:
                    lines += _messages(
                        "Compensation alternative before pivot", process["alternatives"]
                    )
        else:
            lines += [
                p["name"]
                + " · "
                + p["owner"].removeprefix("module:")
                + (f" · v{p['version']}" if "version" in p else "")
                for p in data["processes"]
            ]
            if not data["processes"]:
                lines.append("No declared processes in this scope.")
    return "\n".join(_safe(line) for line in lines) + "\n"
