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
        lines.append("Unresolved: " + str(fact))
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


def render(envelope: Doc, *, overview: bool = False) -> str:
    data, kind = envelope["data"], envelope["kind"]
    lines = [
        HEADER,
        "Freshness: " + freshness_label(envelope["freshness"]["state"]),
        "",
    ]
    if kind == "graph" and overview:
        lines += _overview(data)
    elif kind in {"graph", "modules"}:
        lines += _topology(data)
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
