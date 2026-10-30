"""Evidence-labelled process projections, without loading the application."""

from __future__ import annotations

import re

from sandloom.core.cli.inspection.model import Doc, InspectionError, response
from sandloom.core.cli.inspection.read import Reader

LIFECYCLE = "https://sandloom.dev/docs/concepts/lifecycle/"
SAGAS = "https://sandloom.dev/docs/concepts/sagas/"


def catalog(reader: Reader, module: str | None = None) -> list[Doc]:
    owner = reader.module(module)["id"] if module else None
    return [
        p for p in reader.doc.get("processes", []) if not owner or p["owner"] == owner
    ]


def select(reader: Reader, selector: str, module: str | None = None) -> Doc:
    found = [
        p
        for p in catalog(reader, module)
        if selector in (p["id"], p["name"], p.get("saga_type"))
    ]
    if len(found) != 1:
        raise InspectionError(
            "unknown_process" if not found else "ambiguous_process",
            "Select one stable process ID from graph sequence",
        )
    return found[0]


def _message(source: str, target: str, label: str, **extra: object) -> Doc:
    return {"source": source, "target": target, "label": label, **extra}


def project(process: Doc) -> Doc:
    if process["id"].startswith("saga:"):
        return _saga(process)
    return _route(process)


def _saga(process: Doc) -> Doc:
    owner = process["owner"]
    steps = []
    compensations: list[Doc] = []
    for index, step in enumerate(process["steps"]):
        target = (
            owner if step["local"] else "contract:" + step["call"].rsplit(".", 1)[0]
        )
        steps.append(
            _message(
                "runtime",
                target,
                step["name"],
                kind=step["kind"],
                call=step["call"] or "local callback",
                transaction=step["transaction"],
                source_location=step.get("source"),
                condition="next forward step succeeds; retries bounded by deadlines",
            )
        )
        if step["compensation_available"]:
            compensations.insert(
                0,
                _message(
                    "runtime",
                    owner
                    if step["local"]
                    else "contract:" + step["compensate"].rsplit(".", 1)[0],
                    "Undo " + step["name"],
                    kind="compensation",
                    transaction=step["transaction"],
                    source_location=step.get("compensation_source"),
                    call=step["compensate"] or "local compensation callback",
                    condition=(
                        f"if step {index + 1} completed and compensation "
                        "is entered before pivot"
                    ),
                ),
            )
    return {
        "id": process["id"],
        "name": f"{process['saga_type']} (v{process['version']})",
        "owner": owner,
        "evidence": "declared saga; possible paths, not an observed attempt",
        "steps": steps,
        "alternatives": compensations,
        "notes": [
            (
                "Each step transition has its own unit; provider commands "
                "commit separately."
            ),
            (
                "Transient failures retry until a deadline. Refusal before "
                "pivot can compensate; conflicts and misconfiguration can park."
            ),
            "Local callback bodies and arbitrary nested calls are opaque.",
        ],
        "docs": SAGAS,
        "declaration": process,
    }


def _route(process: Doc) -> Doc:
    policy = process["policy"]
    steps = [_message("caller", "framework", process["name"])]
    session = policy.get("session", "unknown")
    if session not in ("none", None):
        steps.append(
            _message(
                "framework",
                "authorization",
                f"Session policy: {session}",
                condition="refusal stops before handler",
            )
        )
    mode = policy.get("idempotency")
    if mode and not str(mode).startswith("none"):
        steps.append(
            _message(
                "framework",
                "framework",
                f"Idempotency: {mode}",
                condition="replay/conflict can short-circuit the handler",
            )
        )
    intent = bool(policy.get("operation_intent"))
    steps.append(
        _message(
            "framework",
            "handler",
            "Application handler (opaque)",
            condition="no request unit; explicit sequential units"
            if intent
            else (
                "write: eager request unit; read: lazy on first db() or eager "
                "dependency"
            ),
        )
    )
    if policy.get("issues_tokens"):
        steps.append(
            _message(
                "handler",
                "authorization",
                "Live issuance check",
                condition="when tokens are minted; refusal prevents issuance",
            )
        )
    if intent:
        steps.append(
            _message(
                "framework",
                "framework",
                "Validate intent release rule",
                condition=(
                    "2xx only: issue or issued-retry protocol; handler must perform"
                    " release_check"
                ),
            )
        )
    else:
        steps.extend(
            [
                _message(
                    "framework",
                    "authorization",
                    "Commit-time re-check",
                    condition=(
                        "authenticated armed unit only; writes or explicit at_commit; "
                        "refusal rolls back"
                    ),
                ),
                _message(
                    "framework",
                    "unit",
                    "before_commit → COMMIT",
                    condition=(
                        "only if a unit was opened and its body succeeded; exception "
                        "rolls back"
                    ),
                ),
                _message(
                    "unit",
                    "effects",
                    "Outbox dispatch → on_commit",
                    condition=(
                        "queued effects, after successful commit; no consumer "
                        "completion implied"
                    ),
                ),
            ]
        )
    if session == "none":
        steps = [s for s in steps if s["label"] != "Commit-time re-check"]
    steps.append(
        _message(
            "framework",
            "caller",
            "Return response",
            condition="streaming: request unit commits before first byte",
        )
    )
    return {
        "id": process["id"],
        "name": process["name"],
        "owner": process["owner"],
        "evidence": (
            "framework lifecycle from resolved policy; handler internals unknown"
        ),
        "steps": steps,
        "alternatives": [],
        "declaration": process,
        "notes": [
            (
                "Conditional phases are not proof that this handler opens a "
                "unit, publishes an event or calls a contract."
            ),
            "Local/remote command units commit separately from their caller.",
        ],
        "docs": LIFECYCLE,
    }


def build(
    reader: Reader,
    *,
    selector: str | None,
    module: str | None,
    require: bool,
    state: bool = False,
    full: bool = False,
) -> Doc:
    candidates = catalog(reader, module)
    selected = select(reader, selector, module) if selector else None
    if state and (selected is None or not selected["id"].startswith("saga:")):
        raise InspectionError("usage", "Use graph state ID with a saga ID", code=2)
    if selected:
        candidates = [selected]
    owners = (
        {selected["owner"]}
        if selected
        else {reader.module(module)["id"]}
        if module
        else {m["id"] for m in reader.modules}
    )
    data: Doc = {
        "processes": [
            {
                k: p[k]
                for k in ("id", "owner", "name", "evidence_kind", "version")
                if k in p
            }
            for p in candidates
        ],
        "process": project(selected) if selected else None,
    }
    if state:
        data["state"] = states()
    return response(
        "graph",
        "state" if state else "sequence",
        data,
        selection={
            "process_id": selected["id"] if selected else None,
            "module_ids": sorted(owners),
        },
        freshness=reader.freshness(
            [m for m in reader.modules if m["id"] in owners],
            require=require,
            full=full,
            tolerate_missing=True,
        ),
    )


def _label(value: str) -> str:
    # Mermaid sequence entities use #NN; (including #59; for semicolons).
    escaped = "".join(
        f"#{ord(char)};"
        if char in "#&<>\"';`"
        else " "
        if char in "\r\n\u2028\u2029"
        else char
        for char in value
    )
    return re.sub(r"\bend\b", "#101;nd", escaped)


def mermaid(envelope: Doc) -> str:
    data = envelope["data"]
    if "state" in data:
        out = ["stateDiagram-v2", "%% Framework states, not an observed attempt"]
        out += [
            f"  {t['source']} --> {t['target']}: {_label(t['label'])}"
            for t in data["state"]["transitions"]
        ]
        return "\n".join(out) + "\n"
    process = data.get("process")
    if not process:
        raise InspectionError("usage", "Use graph sequence ID --format mermaid", code=2)
    messages = process["steps"] + process["alternatives"]
    participants = sorted({m[end] for m in messages for end in ("source", "target")})
    ids = {name: f"p{i}" for i, name in enumerate(participants)}
    out = ["sequenceDiagram", f"%% {_label(process['evidence'])}", "  autonumber"]
    out += [f"  participant {ids[name]} as {_label(name)}" for name in participants]
    paths = [("Forward path", process["steps"])]
    if process["alternatives"]:
        paths.append(
            (
                "Compensation before pivot: reverse completed steps only",
                process["alternatives"],
            )
        )
    for index, (title, path) in enumerate(paths):
        if len(paths) > 1:
            out.append(f"  {'alt' if index == 0 else 'else'} {title}")
        for item in path:
            arrow = "-->>" if item.get("kind") == "compensation" else "->>"
            pivot = " [PIVOT]" if item.get("kind") == "pivot" else ""
            out.append(
                f"  {ids[item['source']]}{arrow}{ids[item['target']]}: "
                f"{_label(item['label'])}{pivot}"
            )
            facts = [
                str(item[key])
                for key in ("kind", "call", "transaction", "condition")
                if item.get(key)
            ]
            if facts:
                out.append(
                    f"  Note over {ids[item['target']]}: {_label('; '.join(facts))}"
                )
    if len(paths) > 1:
        out.append("  end")
    return "\n".join(out) + "\n"


def states() -> Doc:
    return {
        "evidence": (
            "framework saga states; transitions are conditional, not an "
            "execution history"
        ),
        "transitions": [
            {
                "source": "running",
                "target": "completed",
                "label": "all forward steps succeed",
                "short_label": "all succeed",
            },
            {
                "source": "running",
                "target": "running",
                "label": "advance / retry before deadline",
                "short_label": "retry / advance",
            },
            {
                "source": "running",
                "target": "compensating",
                "label": "refusal / deadline before pivot",
                "short_label": "refuse before pivot",
            },
            {
                "source": "compensating",
                "target": "compensated",
                "label": "undo completed",
                "short_label": "undo completed",
            },
            {
                "source": "compensating",
                "target": "compensating",
                "label": "undo / retry",
                "short_label": "undo / retry",
            },
            {
                "source": "running",
                "target": "parked",
                "label": "unsafe to continue / deadline after pivot",
                "short_label": "park",
            },
            {
                "source": "compensating",
                "target": "parked",
                "label": "unsafe undo / compensation deadline",
                "short_label": "park",
            },
        ],
        "notes": [
            (
                "Parked recovery requires an operator decision; it is not an "
                "automatic retry."
            )
        ],
    }
