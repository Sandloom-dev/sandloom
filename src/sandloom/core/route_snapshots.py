"""Per-module route snapshots: every route a module mounts, and its policy.

Each business module commits ``routes.snapshot.json`` next to its
``contracts.snapshot.json``: for every route its ``register(core)`` mounts,
the method, the path, ``auth`` (``"public"`` or ``"authenticated"``) and a
``policy`` object. The file is generated (``sandloom routes snapshot --write``)
and reviewed like code — its diff IS the approval of a route's exposure and
policy — and a test (or ``sandloom routes snapshot --check``) fails while it is
stale. This replaces one hand-maintained, framework-wide route table: adding
a module touches only the module.

The entry format is deliberately generic: ``policy`` is an open object built
from :data:`POLICY_FIELDS`, the static resolved model E policy of the route
(ADR-0009 item 14: effect, session check, risk with its source, idempotency,
audit, step-up, public reason, write store). The diffs, the reference page
and the checks are field-agnostic.

Attribution: the registration draft stamps every route a module's routers add
with the module name (:data:`MODULE_ATTR`). Routes the KERNEL mounts — the ops
endpoints, the RPC router, the platform console's ``/api/v1/platform/<module>/*``
views — carry no stamp: the kernel decides their policy in its own code and
they are never part of a module snapshot.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from sandloom.core.contract_compat import module_package_dir
from sandloom.core.guard_order import (
    authenticator_kind,
    authorizer_names,
    resolution_order,
)
from sandloom.core.idempotency.durable import guard_mode
from sandloom.core.route_markers import MODULE_ATTR, route_module, stamp_routes
from sandloom.core.route_policy import RoutePolicy, resolve_route_policy

#: File name of a module's route snapshot (in its package directory).
SNAPSHOT_FILENAME = "routes.snapshot.json"
#: Format version of the snapshot document (3: model E, ADR-0009 item 14).
SNAPSHOT_FORMAT = 3
#: Guard kinds of an explicit ``idempotency_guard`` (snapshot spelling).
_GUARD_KINDS = {"durable": "durable", "cache": "cache", "in_flight": "in-flight"}
#: Attribute of the ``at_commit()`` dependency (route runtime).
AT_COMMIT_ATTR = "__sandloom_at_commit__"


def _session(policy: RoutePolicy, route: Any) -> str:
    if policy.auth == "public":
        return "none"
    if policy.auth == "service":
        return "live"
    if policy.operation_intent is not None:
        return "live, release_check"
    if _has_at_commit(route):
        return "live, re-check every unit"
    if policy.is_write:
        return "live, re-check before commit"
    return "live, re-check if a unit writes"


def _has_at_commit(route: Any) -> bool:
    return any(
        getattr(call, AT_COMMIT_ATTR, False)
        for call in resolution_order(route.dependant)
    )


def _idempotency(policy: RoutePolicy, route: Any) -> str:
    for call in resolution_order(route.dependant):
        mode = guard_mode(call)
        if mode is not None:
            closed = ", fail-closed" if mode.fail_closed else ""
            return f"explicit {_GUARD_KINDS.get(mode.kind, mode.kind)}{closed}"
    if policy.no_idempotency is not None:
        return f"none ({policy.no_idempotency})"
    if not policy.is_write or policy.auth != "user":
        return "none"
    if policy.replay_safe is not None:
        return f"automatic B ({policy.replay_safe})"
    return "automatic S"


def _risk(policy: RoutePolicy) -> dict[str, list[str]] | None:
    return {k: list(v) for k, v in policy.risk.items()} or None


def _audit(policy: RoutePolicy) -> str | None:
    return "automatic" if policy.risk and policy.is_write else None


def _multi_db_write(policy: RoutePolicy) -> dict[str, Any] | None:
    decl = policy.multi_db_write
    if decl is None:
        return None
    return {"secondaries": list(decl.secondaries), "reason": decl.reason}


#: ``(field, extractor)`` pairs that make up a route's ``policy`` object: the
#: static resolved model E policy (ADR-0009 item 14). An extractor returning
#: ``None`` leaves its field out.
POLICY_FIELDS: tuple[tuple[str, Callable[[RoutePolicy, Any], Any]], ...] = (
    ("effect", lambda p, r: p.effect),
    ("session", _session),
    ("risk", lambda p, r: _risk(p)),
    ("idempotency", _idempotency),
    ("audit", lambda p, r: _audit(p)),
    ("step_up_max_age_s", lambda p, r: p.step_up_max_age_s),
    ("public_reason", lambda p, r: p.public.reason if p.public is not None else None),
    (
        "issues_tokens",
        lambda p, r: True if p.public is not None and p.public.issues_tokens else None,
    ),
    ("operation_intent", lambda p, r: p.operation_intent),
    ("write_store", lambda p, r: (p.writes or "default") if p.is_write else None),
    ("multi_db_write", lambda p, r: _multi_db_write(p)),
    ("authz", lambda p, r: list(authorizer_names(r.dependant)) or None),
)


def route_auth(route: Any) -> str:
    """``"authenticated"`` when a dependency authenticates a USER (e.g.
    ``current_context``), ``"service"`` when only a system credential is
    required (``@authenticates(kind="service")``), else ``"public"``."""
    kinds = {authenticator_kind(c) for c in resolution_order(route.dependant)}
    if "user" in kinds:
        return "authenticated"
    return "service" if "service" in kinds else "public"


def route_policy(route: Any, method: str | None = None) -> dict[str, Any]:
    """The route's ``policy`` object for ``method`` (see :data:`POLICY_FIELDS`)."""
    resolved = resolve_route_policy(route, method)
    policy = {name: extract(resolved, route) for name, extract in POLICY_FIELDS}
    return {k: v for k, v in policy.items() if v is not None}


def route_entries(routes: Iterable[Any]) -> list[dict[str, Any]]:
    """``[{method, path, auth, policy}]`` of the API routes, sorted."""
    entries = [
        {
            "method": method,
            "path": str(route.path),
            "auth": route_auth(route),
            "policy": route_policy(route, method),
        }
        for route in routes
        if getattr(route, "dependant", None) is not None
        and getattr(route, "methods", None)
        for method in sorted(route.methods)
        if method != "HEAD"
    ]
    return sorted(entries, key=lambda e: (e["path"], e["method"]))


def _describe(entry: Mapping[str, Any]) -> str:
    """``auth, k=v, ...`` — one entry's exposure and policy, field-agnostic."""
    policy = entry.get("policy") or {}
    return ", ".join(
        [str(entry.get("auth")), *(f"{k}={policy[k]}" for k in sorted(policy))]
    )


def module_route_snapshots(
    app: Any, modules: Iterable[str] | None = None, *, observed: bool = False
) -> dict[str, dict[str, Any]]:
    """``{module: snapshot document}`` for the loaded modules of ``app``.

    ``modules`` defaults to every module the app loaded (a module with no
    route still gets a document with an empty list, so a removed router shows
    up as a diff).
    """
    by_module: dict[str, list[Any]] = {}
    for route in getattr(app, "routes", ()):
        module = route_module(route)
        if module is not None:
            by_module.setdefault(module, []).append(route)
    if modules is None:
        summary = getattr(getattr(app, "state", None), "module_summary", None)
        loaded = [d.name for d in getattr(summary, "loaded", ()) or ()]
        modules = loaded or list(by_module)
    docs = {
        module: {
            "format": SNAPSHOT_FORMAT,
            "module": module,
            "routes": route_entries(by_module.get(module, ())),
        }
        for module in sorted(set(modules))
    }
    if observed:
        # ``--write``: attach what the module's tests saw (informational)
        docs = {m: with_observed(d, load_observed(m)) for m, d in docs.items()}
    return docs


def kernel_routes(app: Any) -> list[dict[str, Any]]:
    """The routes the kernel mounted (no module stamp)."""
    return route_entries(r for r in getattr(app, "routes", ()) if not route_module(r))


#: Entry key of the INFORMATIONAL effects the module's own tests saw on a
#: route (ADR-0009 item 14, G-ROUTE-05): written by ``--write``, ignored by
#: the check, never a gate.
OBSERVED_FIELD = "observed_effects"
#: Where the test harness accumulates a module's observations (package dir).
OBSERVED_FILENAME = ".observed_effects.json"


def observed_path(module: str) -> Path | None:
    directory = module_package_dir(module)
    return directory / OBSERVED_FILENAME if directory is not None else None


def load_observed(module: str) -> dict[tuple[str, str], dict[str, list[str]]]:
    """``{(method, path): {risk: [source, ...]}}`` the module's tests recorded
    (empty when none was recorded or the file is unreadable)."""
    path = observed_path(module)
    if path is None or not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {
            (str(e["method"]), str(e["path"])): {
                str(r): sorted(str(s) for s in srcs) for r, srcs in e["effects"].items()
            }
            for e in raw.get("routes", ())
        }
    except (ValueError, KeyError, TypeError, AttributeError):
        return {}


def record_observed(
    module: str, observations: Mapping[tuple[str, str], Mapping[str, Iterable[str]]]
) -> Path | None:
    """Merge ``observations`` into the module's observation file (the test
    harness calls this once per test session; sources accumulate)."""
    path = observed_path(module)
    if path is None or not observations:
        return path
    merged: dict[tuple[str, str], dict[str, set[str]]] = {
        key: {risk: set(srcs) for risk, srcs in effects.items()}
        for key, effects in load_observed(module).items()
    }
    for key, effects in observations.items():
        slot = merged.setdefault(key, {})
        for risk, sources in effects.items():
            slot.setdefault(risk, set()).update(sources)
    doc = {
        "module": module,
        "routes": [
            {
                "method": m,
                "path": p,
                "effects": {r: sorted(s) for r, s in sorted(effects.items())},
            }
            for (m, p), effects in sorted(merged.items())
        ],
    }
    path.write_text(render_snapshot(doc), encoding="utf-8")
    return path


def with_observed(
    doc: Mapping[str, Any],
    observed: Mapping[tuple[str, str], Mapping[str, list[str]]],
) -> dict[str, Any]:
    """``doc`` with each route entry's informational ``observed_effects``."""
    routes = []
    for entry in doc.get("routes", ()):
        effects = observed.get((entry["method"], entry["path"]))
        routes.append({**entry, OBSERVED_FIELD: dict(effects)} if effects else entry)
    return {**doc, "routes": routes}


def _without_observed(doc: Any) -> Any:
    if not isinstance(doc, dict) or not isinstance(doc.get("routes"), list):
        return doc
    return {
        **doc,
        "routes": [
            {k: v for k, v in e.items() if k != OBSERVED_FIELD}
            if isinstance(e, dict)
            else e
            for e in doc["routes"]
        ],
    }


def render_snapshot(doc: Mapping[str, Any]) -> str:
    """Canonical text of a snapshot document (stable diffs)."""
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def snapshot_path(module: str) -> Path | None:
    directory = module_package_dir(module)
    return directory / SNAPSHOT_FILENAME if directory is not None else None


def snapshot_problems(app: Any, modules: Iterable[str] | None = None) -> list[str]:
    """Why the committed snapshots differ from ``app``'s routes (empty: current).

    One line per module, naming the routes added, removed or changed (auth or
    any policy field) and the command that regenerates the file (SANDLOOM-T001).
    """
    problems: list[str] = []
    for module, doc in module_route_snapshots(app, modules).items():
        path = snapshot_path(module)
        if path is None:
            problems.append(f"{module}: cannot locate its package directory")
            continue
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if current is not None and _matches_ignoring_observed(current, doc):
            continue
        problems.append(f"{module}: {path} {_describe_diff(current, doc)}")
    return problems


def _matches_ignoring_observed(current: str, doc: Mapping[str, Any]) -> bool:
    """The committed text equals ``doc`` once the informational
    ``observed_effects`` are set aside (the check never gates on them)."""
    if current == render_snapshot(doc):
        return True
    try:
        parsed = json.loads(current)
    except ValueError:
        return False
    return render_snapshot(_without_observed(parsed)) == render_snapshot(
        _without_observed(dict(doc))
    )


def _describe_diff(current: str | None, doc: Mapping[str, Any]) -> str:
    fix = "run `sandloom routes snapshot --write` and review the diff (SANDLOOM-T001)"
    if current is None:
        return f"is missing — {fix}"
    try:
        old = {
            (e["method"], e["path"]): e
            for e in _without_observed(json.loads(current))["routes"]
        }
    except (ValueError, KeyError, TypeError):
        return f"is not a route snapshot — {fix}"
    new = {(e["method"], e["path"]): e for e in doc["routes"]}
    parts = []
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(k for k in set(old) & set(new) if old[k] != new[k])
    if added:
        parts.append(
            "new: " + ", ".join(f"{m} {p} [{_describe(new[(m, p)])}]" for m, p in added)
        )
    if removed:
        parts.append("removed: " + ", ".join(f"{m} {p}" for m, p in removed))
    if changed:
        parts.append(
            "changed: "
            + ", ".join(
                f"{m} {p} [{_describe(old[(m, p)])}] -> [{_describe(new[(m, p)])}]"
                for m, p in changed
            )
        )
    return f"is stale ({'; '.join(parts) or 'formatting'}) — {fix}"


def render_reference(app: Any) -> str:
    """The generated route reference page (``docs/reference/routes.md``)."""
    out = [
        "# Routes and their policy",
        "",
        "<!-- Generated at docs build (also `sandloom routes reference`). -->",
        "",
        "Every route of the batteries platform: who may call it (`auth`) and "
        "its resolved policy (model E, see "
        "[ADR-0009](../adr/0009-route-policy-and-session-freshness.md)). Each "
        "module approves its own routes in its `routes.snapshot.json` "
        "(`sandloom routes snapshot --write`); the kernel decides the routes it "
        "mounts.",
        "",
    ]
    sections: list[tuple[str, list[dict[str, Any]]]] = [("Kernel", kernel_routes(app))]
    sections += [
        (module, doc["routes"])
        for module, doc in module_route_snapshots(app).items()
        if doc["routes"]
    ]
    for title, entries in sections:
        out += [f"## {title}", "", "| Method | Path | Auth | Policy |"]
        out.append("|---|---|---|---|")
        out += [
            f"| {e['method']} | `{e['path']}` | {e['auth']} | "
            + ", ".join(f"{k}={v}" for k, v in sorted(e["policy"].items()))
            + " |"
            for e in entries
        ]
        out.append("")
    return "\n".join(out)


__all__ = [
    "MODULE_ATTR",
    "OBSERVED_FIELD",
    "OBSERVED_FILENAME",
    "POLICY_FIELDS",
    "SNAPSHOT_FILENAME",
    "AT_COMMIT_ATTR",
    "SNAPSHOT_FORMAT",
    "kernel_routes",
    "load_observed",
    "module_route_snapshots",
    "observed_path",
    "record_observed",
    "render_reference",
    "render_snapshot",
    "route_auth",
    "route_entries",
    "route_module",
    "route_policy",
    "snapshot_path",
    "snapshot_problems",
    "stamp_routes",
    "with_observed",
]
