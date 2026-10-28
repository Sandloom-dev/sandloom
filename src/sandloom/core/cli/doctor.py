"""``sandloom doctor`` — read-only deployment preflight.

Answers "will this process start and work with THIS configuration?" without
serving traffic and without changing anything::

    sandloom doctor                    # human report
    sandloom doctor --json             # machine-readable
    sandloom doctor --skip consumers   # sections: config, modules, infra,
                                  # migrations, consumers, rls, sagas
    sandloom doctor --timeout 10       # per-section budget (seconds)

Sections (each check is ``ok`` / ``warn`` / ``fail`` / ``skip``):

* **config** — the settings load and validate, and pass the checks a process
  runs at boot (:func:`sandloom.core.app_factory.configuration_problems`: JWT
  key material, internal-RPC keys / allowlist); production sanity (explicit
  ``ENVIRONMENT``, ops token, strict-profile problems from
  :func:`sandloom.core.production_profile.config_problems`); every active profile
  override — a semantic setting off its profile value or an accepted risk
  (:func:`sandloom.core.config.tiers.active_overrides`).
* **modules** — the composition root (``SANDLOOM_COMPOSITION_ROOT``) loads; which
  modules are loaded; required consumed contracts are bound.
* **infra** — every configured instance (databases, caches, object stores,
  task queue / broker, event bus) answers its health probe within the
  timeout — the same probes as ``/diagnostics/infra``.
* **migrations** — per database, the applied revision of the core chain and
  of each module chain vs the code's heads
  (:func:`sandloom.core.migrations.migrations.migration_status`; read-only
  connections).
* **consumers** — each durable consumer's group on the event transport:
  exists, pending, lag (Streams only).
* **rls** — with ``tenant_rls`` databases or ``PRODUCTION_PROFILE=
  multi_tenant_strict``: the application role cannot escape Row-Level
  Security and every tenant table is covered (the startup checks).
* **sagas** — the registered saga definitions; per saga database, parked
  instances (they wait for an operator), instances past their deadline and
  active instances of a (type, version) no longer registered (they park as
  ``unknown_definition``). Read in a rolled-back cross-tenant session.

Nothing is written, nothing is refused in production, and no secret is
printed: every message passes :func:`sandloom.core.cli.config.redact_value`.

Exit codes: ``0`` every check ok, ``1`` warnings only, ``2`` at least one
failure (also a usage error).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from sandloom.core.app_factory import configuration_problems
from sandloom.core.cli.config import mask_url_credentials, redact_value
from sandloom.core.cli.migrate import _hosting_databases
from sandloom.core.composition import load_composition_core
from sandloom.core.config import tiers
from sandloom.core.config.common import MULTI_TENANT_STRICT, strict_profile_active
from sandloom.core.config.settings import settings
from sandloom.core.migrations.migrations import migration_status
from sandloom.core.production_profile import (
    config_problems,
    verify_tenant_rls_coverage,
)
from sandloom.core.sagas import ops as saga_ops

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
SECTIONS = (
    "config",
    "modules",
    "infra",
    "migrations",
    "consumers",
    "rls",
    "sagas",
)
EXIT_OK, EXIT_WARN, EXIT_FAIL = 0, 1, 2
_RANK = {SKIP: 0, OK: 0, WARN: 1, FAIL: 2}


@dataclass(slots=True)
class Check:
    section: str
    name: str
    status: str
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(
        self,
        section: str,
        name: str,
        status: str,
        message: str = "",
        **details: Any,
    ) -> None:
        self.checks.append(
            Check(
                section,
                name,
                status,
                mask_url_credentials(message),
                redact_value("details", details) if details else {},
            )
        )

    @property
    def exit_code(self) -> int:
        worst = max((_RANK[c.status] for c in self.checks), default=0)
        return {0: EXIT_OK, 1: EXIT_WARN, 2: EXIT_FAIL}[worst]

    def to_dict(self) -> dict[str, Any]:
        counts = {
            s: sum(c.status == s for c in self.checks) for s in (OK, WARN, FAIL, SKIP)
        }
        return {
            "status": {EXIT_OK: OK, EXIT_WARN: WARN, EXIT_FAIL: FAIL}[self.exit_code],
            "counts": counts,
            "checks": [asdict(c) for c in self.checks],
        }

    def render(self) -> str:
        marks = {OK: "OK  ", WARN: "WARN", FAIL: "FAIL", SKIP: "SKIP"}
        lines: list[str] = []
        section = None
        for c in self.checks:
            if c.section != section:
                section = c.section
                lines.append(f"[{section}]")
            line = f"  {marks[c.status]} {c.name}"
            if c.message:
                line += f" — {c.message}"
            lines.append(line)
        summary = self.to_dict()
        counts = summary["counts"]
        lines.append(
            f"\n{summary['status'].upper()}: {counts[OK]} ok, "
            f"{counts[WARN]} warning(s), {counts[FAIL]} failure(s), "
            f"{counts[SKIP]} skipped"
        )
        return "\n".join(lines)


def _error(exc: BaseException) -> str:
    return mask_url_credentials(f"{type(exc).__name__}: {exc}".strip())


# ---------------------------------------------------------------- config ----


def check_config(report: Report, cfg: Any) -> None:
    report.add(
        "config",
        "settings",
        OK,
        f"ENVIRONMENT={cfg.environment.value}, "
        f"PRODUCTION_PROFILE={getattr(cfg, 'production_profile', 'standard')}",
    )
    check_boot(report, cfg)
    if "environment" not in cfg.model_fields_set:
        report.add(
            "config",
            "environment",
            WARN,
            "ENVIRONMENT is not set (defaults to dev); set it explicitly",
        )
    if cfg.is_prod:
        if not cfg.internal_api_token:
            report.add(
                "config",
                "internal_api_token",
                WARN,
                "INTERNAL_API_TOKEN is unset: /diagnostics, /contracts and "
                "/health details are closed",
            )
        if cfg.docs_exposed:
            report.add("config", "docs", WARN, "API docs are exposed in production")
    check_overrides(report, cfg)
    if getattr(cfg, "production_profile", None) == MULTI_TENANT_STRICT:
        errors, warnings = config_problems(cfg)
        for problem in errors:
            report.add("config", "multi_tenant_strict", FAIL, problem)
        for problem in warnings:
            report.add("config", "multi_tenant_strict", WARN, problem)
        if not errors and not warnings:
            report.add("config", "multi_tenant_strict", OK, "profile requirements met")
        if not cfg.is_prod:
            report.add(
                "config",
                "multi_tenant_strict",
                WARN,
                "the strict profile is enforced only with ENVIRONMENT=prod",
            )


def check_boot(report: Report, cfg: Any) -> None:
    """The configuration checks ``create_app`` runs before it boots."""
    problems = configuration_problems(cfg, include_profile=False)
    for problem in problems:
        report.add("config", "boot", FAIL, problem)
    if not problems:
        report.add("config", "boot", OK, "key material and RPC configuration usable")


def check_overrides(report: Report, cfg: Any) -> None:
    """One check per active profile override (``config_tiers``)."""
    overrides = tiers.active_overrides(cfg)
    for item in overrides:
        loud = cfg.is_prod or item.kind != "weaker"
        report.add(
            "config",
            f"override:{item.setting}",
            WARN if loud else OK,
            item.message(),
            kind=item.kind,
            profile=item.profile,
        )
    if not overrides:
        report.add(
            "config",
            "overrides",
            OK,
            f"no semantic setting overrides the {tiers.profile_of(cfg)} profile",
        )


# --------------------------------------------------------------- modules ----


def check_modules(report: Report, core: Any) -> None:
    modules = sorted(getattr(core, "loaded_modules", []) or [])
    report.add(
        "modules",
        "composition_root",
        OK,
        f"{len(modules)} module(s) loaded: {', '.join(modules) or '-'}",
        modules=modules,
    )

    registry = getattr(core, "service_registry", None)
    local = set(registry.list()) if registry is not None else set()
    remote = set(getattr(settings, "service_bindings", {}) or {})
    for descriptor in getattr(core, "module_descriptors", []) or []:
        for req in getattr(descriptor, "consumes", []) or []:
            if req.name in local or req.name in remote:
                continue
            report.add(
                "modules",
                f"{descriptor.name} -> {req.name}",
                FAIL if req.required else WARN,
                "consumed contract is neither provided locally nor bound in "
                "SERVICE_BINDINGS"
                + ("" if req.required else " (optional: the module degrades)"),
            )


# ----------------------------------------------------------------- infra ----


async def check_infra(report: Report, core: Any, timeout: float) -> None:  # noqa: ASYNC109 - per-check timeout budget from run_doctor
    infra = getattr(core, "infra", None)
    if infra is None:
        report.add("infra", "registry", SKIP, "no infrastructure registry bound")
        return
    try:
        results = await asyncio.wait_for(infra.health_all(), timeout=timeout)
    except TimeoutError:
        report.add(
            "infra", "health", FAIL, f"probes did not finish within {timeout:g}s"
        )
        return
    described = infra.describe().get("instances", {})
    for key in sorted(results):
        result = results[key]
        capability, _, name = key.partition(":")
        driver = described.get(capability, {}).get(name, {}).get("driver", "?")
        details = dict(getattr(result, "details", None) or {})
        status = OK if result.status == "ok" else FAIL
        message = f"driver={driver}"
        if status == FAIL:
            message += f": {details.get('error') or result.status}"
        report.add("infra", key, status, message, **details)


# ------------------------------------------------------------ migrations ----


async def check_migrations(report: Report, core: Any, timeout: float) -> None:  # noqa: ASYNC109 - per-check timeout budget from run_doctor
    descriptors = list(getattr(core, "module_descriptors", []) or [])
    try:
        hosting = _hosting_databases(descriptors)
    except Exception as exc:  # noqa: BLE001 - reported
        report.add("migrations", "hosting_databases", FAIL, _error(exc))
        return
    try:
        rows = await asyncio.wait_for(
            asyncio.to_thread(
                migration_status,
                descriptors,
                hosting,
                connect_timeout=max(1, int(timeout)),
            ),
            timeout=timeout * max(1, len(hosting)) + 1,
        )
    except TimeoutError:
        report.add("migrations", "status", FAIL, f"did not finish within {timeout:g}s")
        return
    except Exception as exc:  # noqa: BLE001 - reported
        report.add("migrations", "status", FAIL, _error(exc))
        return
    for row in rows:
        name = f"{row['chain']}@{row['database']}"
        state = row.get("status")
        applied = ",".join(row.get("applied") or []) or "-"
        heads = ",".join(row.get("code_heads") or []) or "-"
        if state == "ok":
            report.add("migrations", name, OK, f"at head {heads}")
        elif state == "behind":
            report.add(
                "migrations",
                name,
                FAIL,
                f"applied {applied}, code head {heads}: run "
                "`sandloom migrate upgrade-all --apply`",
            )
        elif state == "missing":
            report.add(
                "migrations",
                name,
                FAIL,
                f"{row['version_table']} missing (never migrated): run "
                "`sandloom migrate upgrade-all --apply`",
            )
        elif state == "unknown":
            report.add(
                "migrations",
                name,
                WARN,
                f"applied {applied} is unknown to this code (head {heads}): the "
                "database was migrated by a newer release",
            )
        else:
            report.add("migrations", name, FAIL, str(row.get("error") or state))


# ------------------------------------------------------------- consumers ----


def _consumers(core: Any) -> list[tuple[str, str, bool]]:
    """``(channel, group, required)`` of every durable consumer of ``core``."""
    found: list[tuple[str, str, bool]] = []
    for d in getattr(core, "event_consumers", []) or []:
        found.append((d.channel, d.group_name, bool(getattr(d, "required", True))))  # noqa: PERF401 - cold path (startup / diagnostics)
    registry = getattr(core, "projection_registry", None)
    for d in registry.list_consumers() if registry is not None else []:
        found.append((d.source_channel, d.group_name, True))  # noqa: PERF401 - cold path (startup / diagnostics)
    return sorted(set(found))


async def check_consumers(
    report: Report,
    core: Any,
    timeout: float,  # noqa: ASYNC109 - per-check timeout budget from run_doctor
    *,
    lag_warn: int,
) -> None:
    consumers = _consumers(core)
    if not consumers:
        report.add("consumers", "registered", SKIP, "no durable consumers registered")
        return
    transport = getattr(getattr(core, "event_bus", None), "transport", None)
    group_info = getattr(transport, "group_info", None)
    if transport is None or group_info is None:
        report.add(
            "consumers",
            "transport",
            SKIP,
            f"{len(consumers)} consumer(s); the event bus has no durable "
            "transport (EVENT_BUS_BACKEND=streams reports groups and lag)",
        )
        return
    infos: dict[str, list[dict[str, Any]]] = {}
    for channel, group, required in consumers:
        try:
            if channel not in infos:
                infos[channel] = list(
                    await asyncio.wait_for(group_info(channel), timeout=timeout)
                )
        except Exception as exc:  # noqa: BLE001 - reported
            report.add("consumers", f"{channel} -> {group}", FAIL, _error(exc))
            continue
        info = next((g for g in infos[channel] if g.get("group") == group), None)
        name = f"{channel} -> {group}"
        if info is None:
            report.add(
                "consumers",
                name,
                WARN if required else SKIP,
                "group not created yet (the consumer has never subscribed)",
            )
            continue
        lag, pending = info.get("lag"), info.get("pending")
        status = WARN if isinstance(lag, int) and lag >= lag_warn else OK
        report.add(
            "consumers",
            name,
            status,
            f"pending={pending} lag={lag}",
            pending=pending,
            lag=lag,
        )


# ------------------------------------------------------------------- rls ----


async def check_rls(report: Report, core: Any, timeout: float) -> None:  # noqa: ASYNC109, C901 - per-check timeout budget from run_doctor
    infra = getattr(core, "infra", None)
    strict = strict_profile_active(getattr(core, "settings", None))
    providers: list[tuple[str, Any]] = []
    if infra is not None:
        for name in infra.instances("database"):
            try:
                provider = infra.provider("database", name)
            except Exception as exc:  # noqa: BLE001 - reported
                report.add("rls", f"database/{name}", FAIL, _error(exc))
                continue
            if "tenant_rls" in provider.features() and hasattr(
                provider, "verify_tenant_rls"
            ):
                providers.append((name, provider))
    if not providers and not strict:
        report.add("rls", "tenant_rls", SKIP, "no tenant_rls database configured")
        return
    for name, provider in providers:
        try:
            await asyncio.wait_for(provider.verify_tenant_rls(), timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - reported
            report.add("rls", f"database/{name}", FAIL, _error(exc))
        else:
            report.add(
                "rls", f"database/{name}", OK, "application role cannot escape RLS"
            )
    if strict:
        try:
            problems = await asyncio.wait_for(
                verify_tenant_rls_coverage(core), timeout=timeout
            )
        except Exception as exc:  # noqa: BLE001 - reported
            report.add("rls", "coverage", FAIL, _error(exc))
            return
        for problem in problems:
            report.add("rls", "coverage", FAIL, problem)
        if not problems:
            report.add("rls", "coverage", OK, "every TenantScoped table has forced RLS")


# ---------------------------------------------------------------- sagas ----


async def check_sagas(report: Report, core: Any, budget: float) -> None:
    """Parked / overdue instances and instances of unregistered versions."""
    runtime = getattr(core, "saga_runtime", None)
    registry = getattr(runtime, "registry", None)
    if runtime is None or registry is None or not len(registry):
        report.add("sagas", "definitions", SKIP, "no saga registered in this process")
        return
    report.add(
        "sagas",
        "definitions",
        OK,
        ", ".join(f"{r.saga_type} v{r.version}" for r in registry),
    )
    for database in registry.databases():
        name = f"database/{database}"
        # a failure surfaces through _guarded as this section's FAIL
        found = await asyncio.wait_for(
            saga_ops.attention(runtime, database, actor="cli:doctor"), budget
        )
        problems = []
        if found.parked:
            problems.append(f"{found.parked} parked (they wait for an operator)")
        if found.overdue:
            problems.append(f"{found.overdue} active past their deadline")
        if found.unknown:
            problems.append(
                "active instances of unregistered versions: " + ", ".join(found.unknown)
            )
        if problems:
            report.add(
                "sagas",
                name,
                WARN,
                "; ".join(problems) + " — see `sandloom sagas list`",
                parked=found.parked,
                overdue=found.overdue,
            )
        else:
            report.add("sagas", name, OK, "nothing parked or overdue")


# ------------------------------------------------------------------ main ----


async def _guarded(
    report: Report, section: str, fn: Callable[[], Awaitable[None]]
) -> None:
    try:
        await fn()
    except Exception as exc:  # noqa: BLE001 - a crashing check is a failure
        report.add(section, "check", FAIL, _error(exc))


async def run_doctor(  # noqa: C901
    *,
    skip: frozenset[str] = frozenset(),
    timeout: float = 5.0,  # noqa: ASYNC109 - per-check timeout of the doctor CLI
    lag_warn: int = 1000,
    core_loader: Callable[[], Any] | None = None,
) -> Report:
    """Run every section not in ``skip``; never raises, never writes."""
    report = Report()
    try:
        from sandloom.core.config.settings import settings  # noqa: PLC0415 - cold: doctor turns an invalid configuration into a FAIL line instead of an import traceback
    except SystemExit as exc:  # invalid configuration aborts the import
        report.add("config", "settings", FAIL, str(exc.code or exc))
        return report
    if "config" not in skip:
        check_config(report, settings)

    needs_core = set(SECTIONS) - {"config"} - skip
    if not needs_core:
        return report
    if core_loader is None:
        core_loader = load_composition_core
    try:
        core = core_loader()
    except BaseException as exc:  # SystemExit from a bad module too
        if isinstance(exc, KeyboardInterrupt):
            raise
        report.add("modules", "composition_root", FAIL, _error(exc))
        return report
    try:
        if "modules" not in skip:
            check_modules(report, core)
        if "infra" not in skip:
            await _guarded(report, "infra", lambda: check_infra(report, core, timeout))
        if "migrations" not in skip:
            await _guarded(
                report, "migrations", lambda: check_migrations(report, core, timeout)
            )
        if "consumers" not in skip:
            await _guarded(
                report,
                "consumers",
                lambda: check_consumers(report, core, timeout, lag_warn=lag_warn),
            )
        if "rls" not in skip:
            await _guarded(report, "rls", lambda: check_rls(report, core, timeout))
        if "sagas" not in skip:
            await _guarded(report, "sagas", lambda: check_sagas(report, core, timeout))
    finally:
        infra = getattr(core, "infra", None)
        if infra is not None:
            try:
                await infra.aclose_all()
            except Exception:  # noqa: BLE001, S110 - best-effort teardown
                pass
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sandloom doctor",
        description=(
            "Read-only deployment preflight (exit 0 ok / 1 warnings / 2 failures)"
        ),
    )
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument(
        "--skip",
        default="",
        help=f"comma-separated sections to skip ({', '.join(SECTIONS)})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="per-section probe budget in seconds (default 5)",
    )
    parser.add_argument(
        "--lag-warn",
        type=int,
        default=1000,
        help="warn when a consumer group lags this many entries (default 1000)",
    )
    args = parser.parse_args(argv)
    skip = frozenset(s.strip() for s in args.skip.split(",") if s.strip())
    unknown = sorted(skip - set(SECTIONS))
    if unknown:
        parser.error(f"unknown section(s) {unknown}; choose from {', '.join(SECTIONS)}")
    if not 0 < args.timeout < float("inf"):
        parser.error("--timeout must be a positive number of seconds")
    report = asyncio.run(
        run_doctor(skip=skip, timeout=args.timeout, lag_warn=args.lag_warn)
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        print(report.render())
    return report.exit_code


__all__ = ["Check", "Report", "SECTIONS", "main", "run_doctor"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
