"""The microservices deploy profile as data: topology, invariants and boot.

One model behind three users:

* ``sandloom add deploy`` (:mod:`sandloom.core.cli.scaffold_deploy`) derives each
  service's ``SERVICE_BINDINGS``, ``RPC_ALLOWED_CALLS`` and gateway routes
  from what its modules provide and consume (:func:`service_topology`,
  :func:`expected_bindings`);
* ``sandloom test`` (``schema & deploy``) and the framework's own
  ``test_deploy_profiles`` check a profile against the split invariants
  (:func:`profile_problems`, SANDLOOM-T014): shared settings identical in every
  process (one ``CACHE_URL``), signing material on the token issuer only, a
  database and a migrate one-shot per service, the durable Streams bus,
  bindings exactly the contracts consumed from another service (+ the
  issuer's remote claims), ``RPC_ALLOWED_CALLS`` derived from them, per-service
  RPC keys, liveness never fed by peers, and a gateway that routes every
  module prefix to its service and never ``/_sandloom``, ``/diagnostics`` or
  ``/metrics``;
* the boot check (:func:`boot_problems`): each HTTP service's
  ``SANDLOOM_MODULES`` booted alone with its own environment, as ``sandloom doctor``
  boots a process (settings, startup preflight, composition root, bound
  contracts; no network), in a fresh interpreter per service.

Module topology is read by loading the modules into a fresh, I/O-free facade
(registration only), never by importing another module's code into the
caller. PyYAML (the ``check`` extra) is imported lazily.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from starlette.routing import compile_path

from sandloom.core.descriptors.module_names import module_name as _module_name
from sandloom.core.rpc_identity import allowlist_from_bindings
from sandloom.core.signing_keys import (
    generate_private_key,
    generate_x25519_key,
    private_pem,
    public_pem,
    x25519_private_pem,
    x25519_public_pem,
)
from sandloom.core.tasks.periodic import has_periodic_tasks

PROFILE = "compose/microservices.yml"
GATEWAY = Path("gateway") / "nginx.conf"
CODE = "SANDLOOM-T014"

#: Settings identical in EVERY process of the profile (and so in the shared
#: env anchor): a token minted by one service is verified by all, revocation
#: marks / the bus live in the shared cache, gates share one policy.
SHARED_KEYS: tuple[str, ...] = (
    "ENVIRONMENT",
    "SHUTDOWN_BUDGET_SECONDS",
    "EVENT_PAYLOAD_MAX_BYTES",
    "EVENT_STREAM_RETENTION_SECONDS",
    "EVENT_STREAM_MAXLEN",
    "EVENT_STREAM_PREFIX",
    "EVENT_BUS_BACKEND",
    "CACHE_URL",
    "JWT_ALGORITHM",
    "JWT_PUBLIC_KEYS",
    "JWT_KID",
    "JWT_ISSUER",
    "JWT_AUDIENCE",
    "ACCESS_TOKEN_EXPIRE_MINUTES",
    "REFRESH_TOKEN_EXPIRE_DAYS",
    "INTERNAL_API_TOKEN",
    "RPC_SERVICE_KEYS",
    "RPC_SERVICE_DH_KEYS",
    "RPC_ALLOWED_CALLS",
    "FRONTEND_URL",
    "BILLING_DENY_ON_PAST_DUE",
    "NOTIFIER_BACKEND",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SMTP_FROM",
    "SMTP_USE_TLS",
)

#: Issuer-only signing material: on the token issuer's processes, nowhere else.
ISSUER_ONLY = ("JWT_PRIVATE_KEY", "JWT_SECRET")

#: Paths the gateway must never route (404 at the edge).
INTERNAL_PATHS: tuple[str, ...] = (
    "/_sandloom/rpc/x.y.v1",
    "/diagnostics/core",
    "/metrics",
    "/contracts",
    "/ready",
    "/health/simple",
    "/docs",
    "/openapi.json",
)

#: The first path segments of the internal surfaces: the generated gateway
#: answers them 404 explicitly when a service owns the catch-all ``location
#: /`` (``= /health`` stays the gateway's own probe).
INTERNAL_ROOTS: tuple[str, ...] = (
    "_sandloom",
    "diagnostics",
    "metrics",
    "contracts",
    "health",
    "ready",
    "docs",
    "openapi.json",
    "redoc",
)

#: The gateway prefix of the service serving ``/`` (``location = /``) and of
#: the one serving root-level parameter paths such as ``/{slug}`` (the
#: catch-all ``location /``).
EXACT_ROOT = "= /"
CATCH_ALL = "/"

_VAR = re.compile(r"\$\{(\w+)(?:(:?[-?])([^}]*))?\}")


# --------------------------------------------------------------------------- #
# reading a profile
# --------------------------------------------------------------------------- #


def interpolate(value: object, shell: Mapping[str, str] | None = None) -> str:
    """Compose interpolation: a variable from ``shell``, else its ``:-``
    default, else ``""`` (``${VAR:?msg}`` unset included)."""
    shell = shell or {}

    def repl(m: re.Match[str]) -> str:
        if shell.get(m.group(1)):
            return shell[m.group(1)]
        return m.group(3) or "" if m.group(2) in (":-", "-") else ""

    return _VAR.sub(repl, str(value))


def required_variables(text: str) -> list[str]:
    """Variables the profile requires (``${VAR:?...}``), in order."""
    return list(
        dict.fromkeys(m.group(1) for m in _VAR.finditer(text) if m.group(2) == ":?")
    )


def load_profile(path: Path) -> dict[str, Any]:
    yaml = importlib.import_module("yaml")  # optional: the check extra ships it
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a compose mapping")
    return data


def _env(svc: Any) -> dict[str, Any]:
    env = svc.get("environment") if isinstance(svc, dict) else None
    if isinstance(env, list):
        env = dict(str(item).split("=", 1) for item in env if "=" in str(item))
    return env if isinstance(env, dict) else {}


def _command(svc: Any) -> list[str]:
    cmd = svc.get("command") if isinstance(svc, dict) else None
    if isinstance(cmd, str):
        return cmd.split()
    return [str(part) for part in cmd or []]


def modules_of(svc: Any) -> list[str]:
    raw = interpolate(_env(svc).get("SANDLOOM_MODULES", "")).strip()
    if not raw:
        return []
    try:
        listed = json.loads(raw)
    except ValueError:
        listed = raw.split(",")
    return [str(m).strip() for m in listed if str(m).strip()]


def app_processes(compose: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Every process that runs the application (it lists ``SANDLOOM_MODULES``)."""
    services = compose.get("services") or {}
    return {
        name: svc
        for name, svc in services.items()
        if isinstance(svc, dict) and "SANDLOOM_MODULES" in _env(svc)
    }


def process_kind(svc: Mapping[str, Any]) -> str:
    """``migrate`` | ``worker`` | ``beat`` | ``http``."""
    command = _command(svc)
    if svc.get("restart") == "no" or "migrate" in command:
        return "migrate"
    if command[:4] == ["celery", "-A", "sandloom.core.worker", "worker"]:
        return "worker"
    if command[:4] == ["celery", "-A", "sandloom.core.worker", "beat"]:
        return "beat"
    return "http"


def http_services(compose: Mapping[str, Any]) -> dict[str, list[str]]:
    """HTTP service -> its modules (no migrate one-shot, worker or beat)."""
    return {
        name: modules_of(svc)
        for name, svc in app_processes(compose).items()
        if process_kind(svc) == "http"
    }


def bindings_of(svc: Any) -> dict[str, Any]:
    raw = interpolate(_env(svc).get("SERVICE_BINDINGS", "")).strip() or "{}"
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- #
# module topology
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ServiceTopology:
    """What a module set, loaded alone (registration only), offers and needs."""

    modules: tuple[str, ...]
    provides: frozenset[str] = frozenset()
    #: Consumed contracts not provided by these modules.
    consumes: frozenset[str] = frozenset()
    #: Local token-claims contributors: owner -> critical.
    claims: Mapping[str, bool] = field(default_factory=dict)
    issues_tokens: bool = False
    #: The modules' route paths (+ their platform views) the gateway reaches.
    routes: tuple[str, ...] = ()
    tasks: bool = False
    periodic: bool = False
    error: str | None = None


def _module_dir(module: str) -> Path | None:
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        return None
    locations = list(getattr(spec, "submodule_search_locations", None) or [])
    return Path(locations[0]) if locations else None


def _fixed(path: str) -> tuple[str, ...]:
    """The literal leading segments of a route path (up to a parameter)."""
    out: list[str] = []
    for part in (p for p in path.split("/") if p):
        if part.startswith("{"):
            break
        out.append(part)
    return tuple(out)


def module_routes(paths: list[str], modules: list[str]) -> tuple[str, ...]:
    """The routes a gateway must reach: every route the modules' app serves
    (no docs), root-level ones (``/``, ``/{slug}``) included, plus the
    platform views ``create_app`` mounts per module
    (``/api/v1/platform/<module>``)."""
    out = {f"/api/v1/platform/{_module_name(m)}" for m in modules}
    out |= {p for p in paths if p and not p.startswith(("/docs", "/openapi", "/redoc"))}
    return tuple(sorted(out))


def _root_claims(paths: tuple[str, ...]) -> set[str]:
    """:data:`EXACT_ROOT` / :data:`CATCH_ALL` of the routes without a literal
    leading segment."""
    return {EXACT_ROOT if p == "/" else CATCH_ALL for p in paths if not _fixed(p)}


def gateway_prefixes(
    routes: Mapping[str, tuple[str, ...]],
) -> tuple[dict[str, list[str]], list[str]]:
    """Each service's gateway prefixes and the conflicts.

    A route gets the shortest literal prefix no other service's route shares
    (``/api/v1/<x>``, or the first segment outside ``/api/v1``, made longer
    while another service has routes under it: tenancy's
    ``/api/v1/platform/tenancy`` next to billing's ``/api/v1/platform/billing``).
    Two services with the same literal route prefix conflict (nginx prefix
    locations cannot tell them apart).

    A route without a literal leading segment is root-level: ``/`` gets
    :data:`EXACT_ROOT` (``location = /``), a parameter path such as
    ``/{slug}`` the catch-all :data:`CATCH_ALL` (``location /``: every path no
    other location takes). The gateway has one of each, so two services
    claiming the same one conflict."""
    fixed = {svc: {_fixed(p) for p in paths} - {()} for svc, paths in routes.items()}
    out: dict[str, list[str]] = {}
    conflicts: list[str] = []
    for svc, mine in fixed.items():
        others = {o: gs for o, gs in fixed.items() if o != svc}
        prefixes: set[str] = set()
        for segments in sorted(mine):
            prefix, owners = _shortest_prefix(segments, others)
            if owners:
                conflicts.append(f"{prefix}: {svc} and {', '.join(owners)}")
            else:
                prefixes.add(prefix)
        # a prefix covered by a shorter one of the same service is redundant
        out[svc] = sorted(
            p
            for p in prefixes
            if not any(q != p and p.startswith(q + "/") for q in prefixes)
        )
    for claim, label in ((EXACT_ROOT, "/"), (CATCH_ALL, "/{...}")):
        owners = sorted(
            s for s, paths in routes.items() if claim in _root_claims(paths)
        )
        if len(owners) > 1:
            conflicts.append(f"root-level paths {label}: {' and '.join(owners)}")
        elif owners:
            out[owners[0]] = [claim, *out[owners[0]]]
    return out, sorted(set(conflicts))


def _shortest_prefix(
    segments: tuple[str, ...], others: Mapping[str, set[tuple[str, ...]]]
) -> tuple[str, list[str]]:
    """The shortest prefix of ``segments`` no other service shares, and the
    services that share even the whole literal path (a conflict)."""

    def sharing(depth: int) -> list[str]:
        return sorted(
            o
            for o, gs in others.items()
            if any(g[:depth] == segments[:depth] for g in gs)
        )

    depth = min(3 if segments[:2] == ("api", "v1") else 1, len(segments))
    while depth < len(segments) and sharing(depth):
        depth += 1
    return "/" + "/".join(segments[:depth]), sharing(depth)


def service_topology(modules: list[str]) -> ServiceTopology:
    """Load ``modules`` into a fresh facade (registration only, no I/O)."""
    from sandloom.core.testing.boot import load_topology  # noqa: PLC0415 - cold: builds settings and loads modules - read only when the deploy tooling asks

    core, summary = load_topology(modules, exact=True)
    loaded = {d.name for d in summary.loaded}
    missing = [m for m in modules if m not in loaded]
    if missing or summary.failed:
        failed = [f"{f.name}: {f.error}" for f in summary.failed]
        return ServiceTopology(
            tuple(modules), error=f"not loaded: {missing}; failed: {failed}"
        )
    consumes = {
        entry["contract"]
        for entries in summary.consumes.values()
        for entry in entries
        if entry["resolution"] != "local"
    }
    paths = [str(getattr(r, "path", "")) for r in core.app.router.routes]
    claims = {
        str(owner): bool(critical)
        for owner, critical in core._local_claim_owners().items()
        if owner is not None
    }
    dirs = [_module_dir(m) for m in modules]
    return ServiceTopology(
        modules=tuple(modules),
        provides=frozenset(core.service_registry.list()),
        consumes=frozenset(consumes),
        claims=claims,
        issues_tokens=any(getattr(d, "issues_tokens", False) for d in summary.loaded),
        routes=module_routes(paths, list(modules)),
        tasks=any(d is not None and (d / "tasks.py").is_file() for d in dirs),
        periodic=any(has_periodic_tasks(m) for m in modules),
    )


def binding(service: str, port: int, *, critical: bool = True) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "base_url": f"http://{service}:{port}",
        "service_id": service,
    }
    if not critical:
        entry["critical"] = False
    return entry


def expected_bindings(
    topologies: Mapping[str, ServiceTopology], port: int
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Each service's ``SERVICE_BINDINGS`` and the consumed contracts no
    service provides (``"<service>: <contract>"``, SANDLOOM-T003).

    A service binds exactly the contracts its modules consume and another
    service provides; the token issuer also binds ``<owner>.claims.v1`` of
    every claims contributor deployed elsewhere (``"critical": false`` iff
    the contributor is non-critical)."""
    provider: dict[str, str] = {}
    for name, topo in topologies.items():
        for contract in sorted(topo.provides):
            provider.setdefault(contract, name)
    out: dict[str, dict[str, Any]] = {}
    unresolved: list[str] = []
    for name, topo in topologies.items():
        bound: dict[str, Any] = {}
        for contract in sorted(topo.consumes):
            owner = provider.get(contract)
            if owner is None:
                unresolved.append(f"{name}: {contract}")
            elif owner != name:
                bound[contract] = binding(owner, port)
        if topo.issues_tokens:
            bound |= _remote_claims(name, topologies, port)
        out[name] = dict(sorted(bound.items()))
    return out, unresolved


def _remote_claims(
    issuer: str, topologies: Mapping[str, ServiceTopology], port: int
) -> dict[str, Any]:
    """The issuer's bindings of every claims contributor deployed elsewhere."""
    local = topologies[issuer].claims
    return {
        f"{owner}.claims.v1": binding(other, port, critical=critical)
        for other, peer in topologies.items()
        if other != issuer
        for owner, critical in sorted(peer.claims.items())
        if owner not in local
    }


# --------------------------------------------------------------------------- #
# the gateway
# --------------------------------------------------------------------------- #

_LOCATION_RE = re.compile(
    r"location\s+(=\s+)?(\S+)\s*\{[^}]*?(?:proxy_pass\s+http://([\w.-]+);|return\s+(\d+))"
)


def gateway_locations(text: str) -> list[tuple[bool, str, str | None, str | None]]:
    """``(exact, path, upstream, return status)`` of every nginx location."""
    return [
        (bool(m.group(1)), m.group(2), m.group(3), m.group(4))
        for m in _LOCATION_RE.finditer(text)
    ]


def gateway_route(
    path: str, locations: list[tuple[bool, str, str | None, str | None]]
) -> tuple[str | None, str | None]:
    """nginx prefix matching: exact first, then the longest prefix."""
    for exact, loc, upstream, ret in locations:
        if exact and loc == path:
            return upstream, ret
    best = max(
        (item for item in locations if not item[0] and path.startswith(item[1])),
        key=lambda item: len(item[1]),
        default=None,
    )
    return (best[2], best[3]) if best else (None, None)


#: A route's matchers: ``(segment count or None for a ``:path`` route,
#: specificity, service, route, compiled Starlette regex)``.
_Matcher = tuple[int | None, tuple[int, int], str, str, re.Pattern[str]]


def _segments(path: str) -> list[str]:
    return [p for p in path.split("/") if p]


def route_matchers(routes: Mapping[str, tuple[str, ...]]) -> list[_Matcher]:
    """Every service's routes as Starlette matches them (``compile_path``),
    with their specificity: the literal leading segments, then all literal
    segments (``/shop`` beats ``/{slug}`` on ``/shop``, as nginx's longest
    literal prefix does)."""
    out: list[_Matcher] = []
    for service, paths in routes.items():
        for route in paths:
            parts = _segments(route)
            literal = sum(not p.startswith("{") for p in parts)
            count = None if ":path}" in route else len(parts)
            regex = compile_path(route)[0]
            out.append((count, (len(_fixed(route)), literal), service, route, regex))
    return out


def _literal_segments(routes: Mapping[str, tuple[str, ...]]) -> dict[int, set[str]]:
    """The literal segments of every route, by position."""
    out: dict[int, set[str]] = {}
    for paths in routes.values():
        for route in paths:
            for index, part in enumerate(_segments(route)):
                if not part.startswith("{"):
                    out.setdefault(index, set()).add(part)
    return out


def _plain(route: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", route)


def _sample_paths(route: str, literals: Mapping[int, set[str]]) -> list[str]:
    """Concrete paths of ``route``: every parameter ``x``, then one parameter
    at a time set to a value that collides with a literal segment some route
    has at that position -- the literal itself (``/shop`` for ``/{slug}``)
    and the literal with a suffix (``/shopx``), the paths an unbounded
    nginx prefix location steals."""
    parts = _segments(route)
    plain = ["x" if p.startswith("{") else p for p in parts]
    out = [_plain(route)]
    for index, part in enumerate(parts):
        if not part.startswith("{"):
            continue
        out.extend(
            "/" + "/".join([*plain[:index], value, *plain[index + 1 :]])
            for literal in sorted(literals.get(index, ()))
            for value in (literal, f"{literal}x")
        )
    return out


def route_owner(path: str, matchers: list[_Matcher]) -> tuple[str, str] | None:
    """``(service, route)`` whose app serves ``path`` -- the most specific
    match; ``None`` when no service, or several equally (the shared and
    internal surfaces every service serves), serve it."""
    parts = _segments(path)
    if parts and parts[0] in INTERNAL_ROOTS:
        return None
    count = len(parts)
    best: list[tuple[tuple[int, int], str, str]] = []
    for segments, rank, service, route, regex in matchers:
        if segments not in (None, count) or not regex.match(path):
            continue
        if not best or rank > best[0][0]:
            best = [(rank, service, route)]
        elif rank == best[0][0]:
            best.append((rank, service, route))
    services = {service for _, service, _ in best}
    if len(services) != 1:
        return None
    return best[0][1], best[0][2]


def _upstream_hosts(text: str) -> dict[str, str]:
    """``upstream <name> { server <host>:<port> ...}`` -> name -> host."""
    return dict(
        re.findall(r"upstream\s+([\w.-]+)\s*\{[^}]*?server\s+([\w.-]+)", text, re.S)
    )


# --------------------------------------------------------------------------- #
# the invariants (SANDLOOM-T014)
# --------------------------------------------------------------------------- #


@dataclass
class ProfileReport:
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    topologies: dict[str, ServiceTopology] = field(default_factory=dict)


def profile_problems(
    deploy_dir: Path, *, served: list[str] | None = None
) -> ProfileReport:
    """Check ``deploy_dir``'s microservices profile and gateway.

    ``served``: the modules the application loads; one no service deploys
    is a warning (SANDLOOM-T003: unreachable when split)."""
    profile = deploy_dir / PROFILE
    report = ProfileReport()
    where = f"{deploy_dir.name}/{PROFILE}"

    def problem(text: str) -> None:
        report.problems.append(f"{where}: {text} ({CODE})")

    compose = load_profile(profile)
    procs = app_processes(compose)
    http = http_services(compose)
    if not http:
        problem("no HTTP service lists SANDLOOM_MODULES")
        return report
    _shared_settings(procs, problem)
    for name, modules in http.items():
        report.topologies[name] = topo = service_topology(modules)
        if topo.error:
            problem(f"service {name!r} does not load: {topo.error}")
    _issuer_only(procs, report.topologies, problem)
    _databases_and_migrations(compose, http, problem)
    _bindings(compose, http, report, problem)
    _keys_and_probes(compose, http, problem)
    _gateway(deploy_dir, http, report.topologies, problem)
    hosted = [m for modules in http.values() for m in modules]
    for module in sorted({m for m in hosted if hosted.count(m) > 1}):
        problem(f"{module} is served by more than one HTTP service")
    for module in sorted(set(served or ()) - set(hosted)):
        report.warnings.append(
            f"{where}: {module} is not deployed (unreachable when split): "
            f"`sandloom add deploy {_module_name(module)} --service <name>` "
            "adds it (SANDLOOM-T003)"
        )
    return report


def _shared_settings(procs: dict[str, dict[str, Any]], problem: Any) -> None:
    for key in SHARED_KEYS:
        values = {name: _env(svc).get(key) for name, svc in procs.items()}
        missing = sorted(n for n, v in values.items() if v is None)
        if missing:
            problem(
                f"{key} is missing in {missing}: shared settings live in the "
                "shared env anchor (x-common-env), merged by every process"
            )
        elif len({json.dumps(v, sort_keys=True) for v in values.values()}) > 1:
            seen = sorted(set(map(str, values.values())))
            problem(
                f"{key} differs between processes ({seen}): it must be "
                "identical everywhere (keep it in x-common-env only)"
            )
    for name, svc in procs.items():
        env = _env(svc)
        if interpolate(env.get("ENVIRONMENT", "")) != "prod":
            problem(f"{name}: ENVIRONMENT must be prod")
        if interpolate(env.get("EVENT_BUS_BACKEND", "auto")) not in {
            "streams",
            "auto",
        }:
            problem(f"{name}: events cross services: EVENT_BUS_BACKEND=streams")
        if ":?" not in str(env.get("INTERNAL_API_TOKEN", "")):
            problem(f"{name}: INTERNAL_API_TOKEN must be required (${{...:?...}})")


def _issuer_only(
    procs: dict[str, dict[str, Any]],
    topologies: Mapping[str, ServiceTopology],
    problem: Any,
) -> None:
    issuer_modules = {
        m for topo in topologies.values() if topo.issues_tokens for m in topo.modules
    }
    issuers = [n for n, t in topologies.items() if t.issues_tokens]
    if len(issuers) > 1:
        problem(f"more than one service issues tokens: {issuers}")
    for name, svc in procs.items():
        issuer = bool(set(modules_of(svc)) & issuer_modules)
        env = _env(svc)
        for key in ISSUER_ONLY:
            if issuer and key not in env:
                problem(f"{name}: the token issuer's processes need {key}")
            elif key in env and not issuer:
                problem(
                    f"{name}: {key} belongs to the token issuer's processes "
                    "only (a verify-only service must not be able to mint)"
                )


def _databases_and_migrations(
    compose: Mapping[str, Any], http: Mapping[str, list[str]], problem: Any
) -> None:
    services = compose.get("services") or {}
    urls: dict[str, str] = {}
    for name in http:
        env = _env(services[name])
        url = interpolate(env.get("DATABASE_URL", ""))
        urls[name] = url
        migrates = [
            other
            for other, svc in services.items()
            if isinstance(svc, dict)
            and process_kind(svc) == "migrate"
            and interpolate(_env(svc).get("DATABASE_URL", "")) == url
            and modules_of(svc) == http[name]
        ]
        if not migrates:
            problem(f"{name}: no migrate one-shot for its database and modules")
            continue
        depends = services[name].get("depends_on") or {}
        if not any(
            isinstance(depends, dict)
            and (depends.get(m) or {}).get("condition")
            == "service_completed_successfully"
            for m in migrates
        ):
            problem(f"{name}: does not wait for {migrates[0]} to complete")
    dbs = {n: u.rsplit("/", 1)[-1] for n, u in urls.items()}
    if len(set(dbs.values())) != len(dbs):
        problem(f"services share a database: {dbs}")
    db = services.get("db") if isinstance(services.get("db"), dict) else None
    if db is not None:
        created = set(
            interpolate(_env(db).get("POSTGRES_MULTIPLE_DATABASES", "")).split()
        )
        if created != set(dbs.values()):
            problem(
                f"POSTGRES_MULTIPLE_DATABASES {sorted(created)} != the services' "
                f"databases {sorted(set(dbs.values()))}"
            )


def _bindings(
    compose: Mapping[str, Any],
    http: Mapping[str, list[str]],
    report: ProfileReport,
    problem: Any,
) -> None:
    services = compose.get("services") or {}
    topologies = report.topologies
    if any(t.error for t in topologies.values()):
        return
    port = 0
    expected, unresolved = expected_bindings(topologies, port)
    for item in unresolved:
        report.warnings.append(
            f"no deployed service provides the contract {item.split(': ', 1)[1]} "
            f"consumed by {item.split(': ', 1)[0]}: deploy its module "
            "(`sandloom add deploy`) or it is unbound when split (SANDLOOM-T003)"
        )
    actual: dict[str, dict[str, Any]] = {}
    for name in http:
        try:
            bound = bindings_of(services[name])
        except ValueError as exc:
            problem(f"{name}: SERVICE_BINDINGS is not JSON ({exc})")
            continue
        actual[name] = bound
        _service_bindings(name, bound, expected[name], problem)
    _allowlist(compose, actual, problem)


def _service_bindings(
    name: str, bound: Mapping[str, Any], want: Mapping[str, Any], problem: Any
) -> None:
    if set(bound) != set(want):
        problem(
            f"{name}: SERVICE_BINDINGS binds {sorted(bound)}, its modules "
            f"need {sorted(want)} (`sandloom add deploy --refresh` re-derives them)"
        )
    for contract, entry in bound.items():
        target = want.get(contract)
        if target is None or not isinstance(entry, dict):
            continue
        owner = target["service_id"]
        url = str(entry.get("base_url") or entry.get("url") or "")
        if entry.get("service_id") != owner or not re.match(
            rf"https?://{re.escape(owner)}(:\d+)?/?$", url
        ):
            problem(f"{name}: {contract} must bind the service {owner!r}")
        if entry.get("critical", True) is not target.get("critical", True):
            problem(
                f"{name}: {contract} critical={entry.get('critical', True)}, "
                f"its contributor is critical={target.get('critical', True)}"
            )


def _allowlist(
    compose: Mapping[str, Any], actual: Mapping[str, Any], problem: Any
) -> None:
    procs = app_processes(compose)
    raw = {interpolate(_env(s).get("RPC_ALLOWED_CALLS", "{}")) for s in procs.values()}
    want_allowed = allowlist_from_bindings(actual)
    for value in raw:
        try:
            allowed = json.loads(value or "{}")
        except ValueError:
            problem("RPC_ALLOWED_CALLS is not JSON")
            continue
        if allowed != want_allowed:
            problem(
                f"RPC_ALLOWED_CALLS {allowed} != the services' bindings "
                f"{want_allowed} (deny by default: exactly what each caller binds)"
            )


def _keys_and_probes(
    compose: Mapping[str, Any], http: Mapping[str, list[str]], problem: Any
) -> None:
    services = compose.get("services") or {}
    for name in http:
        svc = services[name]
        env = _env(svc)
        if interpolate(env.get("RPC_SERVICE_ID", "")) != name:
            problem(f"{name}: RPC_SERVICE_ID must be {name!r}")
        if "RPC_DH_PRIVATE_KEY" not in env:
            problem(f"{name}: every service holds its own RPC_DH_PRIVATE_KEY")
        try:
            calls = bool(bindings_of(svc))
        except ValueError:
            calls = False
        if calls and "RPC_PRIVATE_KEY" not in env:
            problem(f"{name} calls other services: it needs RPC_PRIVATE_KEY")
        test = " ".join(map(str, (svc.get("healthcheck") or {}).get("test") or []))
        if "/ready" in test or ("/health" in test and "/health/simple" not in test):
            problem(
                f"{name}: the container healthcheck (liveness) must probe "
                "/health/simple: a peer outage must never restart a service "
                "(peers feed /ready only)"
            )
    _workers(compose, problem)


_WORKER_HEALTHCHECK = ["CMD", "python", "-m", "sandloom.core.worker", "healthcheck"]


def _workers(compose: Mapping[str, Any], problem: Any) -> None:
    for name, svc in app_processes(compose).items():
        if process_kind(svc) != "worker":
            continue
        if "WORKER_CONCURRENCY" not in _env(svc):
            problem(f"{name}: size the worker (WORKER_CONCURRENCY)")
        hc = svc.get("healthcheck") or {}
        if hc.get("disable") or hc.get("test") != _WORKER_HEALTHCHECK:
            problem(
                f"{name}: health-check the worker (sandloom.core.worker healthcheck)"
            )


def _misroutes(
    served: Mapping[str, tuple[str, ...]],
    locations: list[tuple[bool, str, str | None, str | None]],
    hosts: Mapping[str, str],
) -> list[str]:
    """Every sampled path (:func:`_sample_paths`) the gateway sends elsewhere
    than to the service whose app routes match it (:func:`route_owner`)."""
    matchers = route_matchers(served)
    literals = _literal_segments(served)
    out: dict[str, str] = {}
    for name, routes in served.items():
        for route in routes:
            for concrete in _sample_paths(route, literals):
                owner = route_owner(concrete, matchers)
                if owner is None:
                    continue
                expected, matched = owner
                upstream, _ = gateway_route(concrete, locations)
                if hosts.get(upstream or "", upstream) == expected:
                    continue
                shown = (
                    f"{route} ({name})"
                    if concrete == _plain(route)
                    else f"{concrete} ({expected}: {matched})"
                )
                out.setdefault(
                    shown,
                    f"the gateway routes {shown} to {upstream}: route it "
                    f"to {expected} (`sandloom add deploy --refresh`)",
                )
    return list(out.values())


def _gateway(
    deploy_dir: Path,
    http: Mapping[str, list[str]],
    topologies: Mapping[str, ServiceTopology],
    problem: Any,
) -> None:
    path = deploy_dir / GATEWAY
    if not path.is_file():
        problem(f"no gateway ({GATEWAY}): every module prefix must be routed")
        return
    text = path.read_text(encoding="utf-8")
    locations = gateway_locations(text)
    served = {n: t.routes for n, t in topologies.items() if not t.error}
    for misroute in _misroutes(served, locations, _upstream_hosts(text)):
        problem(misroute)
    for internal in INTERNAL_PATHS:
        upstream, ret = gateway_route(internal, locations)
        if upstream is not None or ret != "404":
            problem(f"the gateway exposes {internal}: it must answer 404")
    if not re.search(r"^\s*resolver\s+127\.0\.0\.11\b.*valid=", text, re.M):
        problem("the gateway must re-resolve services (resolver 127.0.0.11 valid=)")
    servers = re.findall(r"^\s*server\s+([\w.-]+:\d+)([^;]*);", text, re.M)
    if not servers or not all("resolve" in opts for _, opts in servers):
        problem("every gateway upstream server needs `resolve`")
    del http


# --------------------------------------------------------------------------- #
# development keys and the boot check
# --------------------------------------------------------------------------- #


def dev_shell(compose_text: str) -> dict[str, str]:
    """Fresh random values for every variable the profile requires that a
    key generator can make (DEVELOPMENT / CI ONLY: written in plain text).

    PEMs keep real newlines here; :func:`env_file_text` escapes them."""
    required = required_variables(compose_text)
    shell: dict[str, str] = {}
    jwt = generate_private_key("EdDSA")
    callers: dict[str, str] = {}
    dh: dict[str, str] = {}
    for var in required:
        m = re.fullmatch(r"([A-Z0-9_]+?)_RPC(_DH)?_PRIVATE_KEY", var)
        if not m:
            continue
        service = m.group(1).lower()
        if m.group(2):
            key = generate_x25519_key()
            shell[var] = x25519_private_pem(key)
            shell[f"{m.group(1)}_RPC_DH_KEY_ID"] = f"{service}-dh-dev"
            dh[service] = x25519_public_pem(key)
        else:
            signing = generate_private_key("EdDSA")
            shell[var] = private_pem(signing)
            shell[f"{m.group(1)}_RPC_KEY_ID"] = f"{service}-dev"
            callers[service] = public_pem(signing)
    fixed = {
        "JWT_PRIVATE_KEY": private_pem(jwt),
        "JWT_PUBLIC_KEYS": json.dumps({"jwt-dev": public_pem(jwt)}),
        "JWT_KID": "jwt-dev",
        "JWT_SECRET": secrets.token_hex(32),
        "INTERNAL_API_TOKEN": secrets.token_hex(32),
        "TWOFA_ENCRYPTION_KEY": secrets.token_hex(32),
        "RPC_SERVICE_KEYS": json.dumps(
            {s: {f"{s}-dev": pem} for s, pem in sorted(callers.items())}
        ),
        "RPC_SERVICE_DH_KEYS": json.dumps(
            {s: {f"{s}-dh-dev": pem} for s, pem in sorted(dh.items())}
        ),
        # A demo stack on the fake PSP (production chooses stripe).
        "BILLING_PROVIDER": "fake",
    }
    for var in required:
        if var in fixed:
            shell[var] = fixed[var]
    if "BILLING_PROVIDER" in shell:
        shell["SANDLOOM_ACCEPTED_RISKS"] = "local_storage_in_prod,fake_psp_in_prod"
    return shell


def env_file_text(shell: Mapping[str, str], compose_text: str) -> str:
    """A compose ``--env-file`` (single-quoted: no escapes, no ``$``
    expansion; PEM newlines as ``\\n``, which the key loaders accept)."""
    lines = [
        "# DEVELOPMENT / CI ONLY — generated by `sandloom keys dev-env`: fresh random",
        "# keys in a plain file. Never use them in production (sandloom keys generate,",
        "# a secret store). Use: docker compose --env-file <this file> -f "
        f"{PROFILE} up",
        "# A variable exported in your shell overrides this file (compose).",
    ]
    for key, value in shell.items():
        lines.append(f"{key}='{value.replace(chr(10), chr(92) + 'n')}'")
    lines += [
        f"# {var}=   (required by the profile: set it)"
        for var in required_variables(compose_text)
        if var not in shell
    ]
    return "\n".join(lines) + "\n"


def parse_env_file(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key.strip()] = value
    return out


_DOCTOR = (
    "import sys\n"
    "from sandloom.core.cli.doctor import main\n"
    "raise SystemExit(main(['--json', '--skip', "
    "'infra,migrations,consumers,rls,sagas']))\n"
)


def boot_service(
    env: Mapping[str, str], *, python: str | None = None, pythonpath: str = ""
) -> list[str]:
    """Boot one process environment as ``sandloom doctor`` does (settings, the
    startup preflight, the composition root, its bound contracts; no
    network), in a fresh interpreter whose environment is exactly ``env``.
    Returns the failures (a doctor failure, or a consumed contract left
    unbound)."""
    base = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
    if pythonpath:
        base["PYTHONPATH"] = pythonpath
    with tempfile.TemporaryDirectory(prefix="sandloom-boot-") as cwd:  # no .env here
        proc = subprocess.run(  # noqa: S603 - this interpreter, fixed code
            [python or sys.executable, "-c", _DOCTOR],
            env={**base, **env},
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    try:
        report = json.loads(proc.stdout)
    except ValueError:
        return [f"did not boot: {(proc.stderr or proc.stdout).strip()[-1500:]}"]
    failures: list[str] = []
    for check in report.get("checks", []):
        status, section = check.get("status"), check.get("section")
        if status == "fail" or (status == "warn" and section == "modules"):
            failures.append(
                f"{section}/{check.get('name')}: {check.get('message') or check}"
            )
    return failures


def boot_problems(
    deploy_dir: Path,
    shell: Mapping[str, str],
    *,
    python: str | None = None,
    pythonpath: str = "",
) -> dict[str, list[str]]:
    """Every HTTP service of the profile booted alone (:func:`boot_service`)
    with its interpolated environment; ``{service: failures}``."""
    compose = load_profile(deploy_dir / PROFILE)
    services = compose.get("services") or {}
    out: dict[str, list[str]] = {}
    for name in http_services(compose):
        raw = _env(services[name])
        unset = sorted(
            {
                m.group(1)
                for v in raw.values()
                for m in _VAR.finditer(str(v))
                if m.group(2) == ":?" and not shell.get(m.group(1))
            }
        )
        if unset:
            out[name] = [f"the profile requires {unset}"]
            continue
        env = {k: interpolate(v, shell) for k, v in raw.items()}
        out[name] = boot_service(env, python=python, pythonpath=pythonpath)
    return out


__all__ = [
    "CODE",
    "GATEWAY",
    "INTERNAL_PATHS",
    "ISSUER_ONLY",
    "PROFILE",
    "SHARED_KEYS",
    "ProfileReport",
    "ServiceTopology",
    "app_processes",
    "binding",
    "bindings_of",
    "boot_problems",
    "boot_service",
    "dev_shell",
    "env_file_text",
    "expected_bindings",
    "gateway_locations",
    "gateway_route",
    "http_services",
    "interpolate",
    "load_profile",
    "modules_of",
    "parse_env_file",
    "process_kind",
    "profile_problems",
    "required_variables",
    "gateway_prefixes",
    "CATCH_ALL",
    "EXACT_ROOT",
    "INTERNAL_ROOTS",
    "module_routes",
    "service_topology",
    "route_matchers",
    "route_owner",
]
