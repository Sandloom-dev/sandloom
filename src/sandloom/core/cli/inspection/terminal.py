"""Plain, deterministic terminal summaries over the same inspection envelope."""

from __future__ import annotations

from sandloom.core.cli.inspection.model import Doc

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
        children = [
            f"{edge['kind'].replace('_', ' ')}: "
            f"{nodes.get(edge['source'], edge['source'])} → "
            f"{nodes.get(edge['target'], edge['target'])}"
            + (" [optional]" if edge.get("required") is False else "")
            for edge in data.get("edges", [])
            if edge["owner"] == module["id"]
            and edge["kind"] not in {"owns_consumer", "owns_projection"}
        ]
        lines += _tree(module.get("name", module["id"]), children)
    for cycle in data.get("cycles", []):
        lines.append(f"Cycle ({cycle['kind']}): " + ", ".join(cycle["module_ids"]))
    for fact in data.get("unresolved", []):
        lines.append("Unresolved: " + str(fact))
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


def render(envelope: Doc) -> str:
    data, kind = envelope["data"], envelope["kind"]
    lines = [HEADER, "Freshness: " + envelope["freshness"]["state"], ""]
    if kind in {"graph", "modules"}:
        lines += _topology(data)
    elif kind == "impact":
        lines += ["Potential impact: " + data["item"]["id"], data["meaning"]]
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
            lines += [p["id"] + " · " + p["name"] for p in data["processes"]]
            if not data["processes"]:
                lines.append("No declared processes in this scope.")
    lines += ["", "Open the interactive view: sandloom explore"]
    return "\n".join(_safe(line) for line in lines) + "\n"
