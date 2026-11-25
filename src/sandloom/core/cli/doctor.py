"""``sandloom doctor`` — read-only deployment preflight.

Answers "will this process start and work with THIS configuration?" without
serving traffic and without changing anything::

    sandloom doctor                    # human report
    sandloom doctor --json             # machine-readable
    sandloom doctor --skip consumers   # sections: config, modules, infra,
                                  # periodic, migrations, consumers, rls,
                                  # sagas
    sandloom doctor --timeout 10       # per-section budget (seconds)

Sections (each check is ``ok`` / ``warn`` / ``fail`` / ``skip``):

* **config** — the settings load and validate, and pass the checks a process
  runs at boot (:func:`sandloom.core.app_factory.configuration_problems`: JWT
  key material, internal-RPC keys / allowlist); production sanity (explicit
  ``ENVIRONMENT``, ops token, strict-profile problems from
  :func:`sandloom.core.production_profile.config_problems`); every active profile
  override — a semantic setting off its profile value or an accepted risk
  (:func:`sandloom.core.config.tiers.active_overrides`), and the same for the
  loaded modules' declared settings and risks
  (:func:`check_module_settings`); the composition loads in collect mode
  (ADR-0015 decision 8), so every module's refusal is one failed check.
* **modules** — the composition root (``SANDLOOM_COMPOSITION_ROOT``) loads; which
  modules are loaded; required consumed contracts are bound.
* **infra** — every configured instance (databases, caches, object stores,
  task queue / broker, event bus) answers its health probe within the
  timeout — the same probes as ``/diagnostics/infra`` (the ``celery``
  taskqueue's probe connects to its broker). Each row names the driver and its
  tier (``experimental``, ``beta``, ``stable``).
* **periodic** — each loaded module whose taskqueue requirement asks for
  ``periodic``: whether a beat schedules its jobs for this composition (the
  beat's singleton lease is held). Missing for a required requirement fails;
  for an optional one (identity's token cleanup) warns.
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

import sandloom.core.infra.fenced_lease as _beat_lease
from sandloom.core.app_factory import configuration_problems
from sandloom.core.cli.config import redact_value
from sandloom.core.cli.migrate import _hosting_databases
from sandloom.core.cli.url_text import mask_url_credentials
from sandloom.core.composition import load_composition_core
from sandloom.core.config import module_settings as _module_settings
from sandloom.core.config import tiers
from sandloom.core.config.common import MULTI_TENANT_STRICT, strict_profile_active
from sandloom.core.config.settings import misspelled_settings_in_use, settings
from sandloom.core.eventing.bus_base import BaseEventBus, durable_transport
from sandloom.core.infra.errors import InvalidArgument
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
    "periodic",
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
    if not cfg.environment_explicit:
        report.add(
            "config",
            "environment",
            WARN,
            "ENVIRONMENT is not set: running with dev defaults (a remote database "
            "is refused, SANDLOOM-B043, and a dev token is not accepted); set "
            "ENVIRONMENT=prod for a deploy, ENVIRONMENT=dev to silence this",
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
    check_misspelled_settings(report)
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


def check_misspelled_settings(report: Report) -> None:
    """A near miss of a framework setting in the environment or ``.env`` is
    ignored by the settings model (production refuses it): warn in every
    environment."""
    for name, suggestion in misspelled_settings_in_use():
        report.add(
            "config",
            "misspelled_setting",
            WARN,
            f"{name} is not a framework setting and is ignored; did you mean "
            f"{suggestion}?",
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


def _add_diagnosed(report: Report, diagnosed: list[_module_settings.Diagnosed]) -> None:
    for item in diagnosed:
        report.add(
            "config",
            f"module:{item.module}",
            FAIL,
            mask_url_credentials(item.message),
        )


def check_module_settings(report: Report, core: Any) -> None:
    """What the loaded modules declare (ADR-0015): each semantic setting off
    its profile value and each active accepted risk, as the kernel's
    (:func:`check_overrides`), and the module-namespaced risk ids that do not
    apply here (their module is not loaded in this process)."""
    registry = getattr(core, "module_settings", None)
    if registry is None:
        return
    cfg = settings
    for item in registry.overrides:
        loud = cfg.is_prod or item.kind != "weaker"
        report.add(
            "config",
            f"override:{item.setting}",
            WARN if loud else OK,
            item.message(),
            kind=item.kind,
            profile=item.profile,
        )
    for risk_id in registry.risk_report.inert:
        report.add(
            "config",
            f"risk:{risk_id}",
            OK,
            f"{risk_id} is not applicable here: its module is not loaded in "
            "this process",
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
    for descriptor in getattr(core, "module_descriptors", []) or []:
        if not getattr(descriptor, "issues_tokens", False):
            continue
        # The issuer's declaration (ADR-0012 decision 5): checked at startup
        # (SANDLOOM-B035), shown here so an operator sees which applies.
        holds = bool(getattr(descriptor, "refresh_sessions", True))
        report.add(
            "modules",
            f"{descriptor.name} issues tokens",
            OK,
            "holds refresh sessions: a block ends them "
            "(durable consumer of core.auth.user_blocked.v1)"
            if holds
            else "declares refresh_sessions=False: a stateless issuer "
            "(no refresh sessions to end on a block)",
            refresh_sessions=holds,
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
        row = described.get(capability, {}).get(name, {})
        driver = row.get("driver", "?")
        details = dict(getattr(result, "details", None) or {})
        details["tier"] = row.get("tier")
        status = OK if result.status == "ok" else FAIL
        message = f"driver={driver} tier={row.get('tier') or '?'}"
        if status == FAIL:
            message += f": {details.get('error') or result.status}"
        report.add("infra", key, status, message, **details)


# -------------------------------------------------------------- periodic ----


def _holder_identity(token: str) -> str:
    """``<host>:<pid>`` of a lease holder ``<fence>:<host>:<pid>``."""
    parts = token.split(":")
    return ":".join(parts[1:]) if len(parts) >= 3 else token


async def check_periodic(report: Report, core: Any, budget: float) -> None:
    """Whether a beat schedules the periodic jobs the loaded modules need.

    Keyed on each module's effective taskqueue requirement with feature
    ``periodic``. Without a beat, a required one fails and an optional one
    warns (the module degrades without its jobs; its docs say how). The beat
    is seen through its singleton lease for THIS composition (one cache read
    per taskqueue instance). A held lease shows a beat schedules the jobs, not
    that a worker runs them: a module that must know (billing's reconciler)
    watches its own runs in its readiness check.
    """
    needs = _beat_lease.periodic_needs(
        getattr(core, "module_descriptors", []) or [],
        getattr(core, "loaded_modules", []) or [],
    )
    if not needs:
        report.add(
            "periodic", "periodic jobs", SKIP, "no loaded module needs periodic jobs"
        )
        return
    if not hasattr(core, "cache"):
        report.add("periodic", "periodic jobs", SKIP, "no infrastructure bound")
        return
    for instance in sorted({need.instance for need in needs}):
        state, value = await _beat_lease.read_beat_lease(
            core, instance=instance, limit_seconds=budget
        )
        for need in (n for n in needs if n.instance == instance):
            report.add(
                "periodic",
                f"{need.module} periodic jobs",
                *_periodic_verdict(state, value, instance, required=need.required),
                instance=instance,
                required=need.required,
            )


def _periodic_verdict(
    state: str, value: str | None, instance: str, *, required: bool
) -> tuple[str, str]:
    """``(status, message)`` of one module's periodic need."""
    if state == "held":
        return OK, (
            f"a beat schedules them on taskqueue/{instance} (lease held by "
            f"{_holder_identity(value or '')})"
        )
    if state == "unknown":
        return WARN, value or "the beat lease cannot be read"
    absent = f"no beat holds the lease of this module set on taskqueue/{instance}: "
    if required:
        return FAIL, absent + (
            "they do not run (required: start a worker and one `sandloom beat`)"
        )
    return WARN, absent + (
        "they do not run (optional: the module works without them and degrades "
        "as its docs describe)"
    )


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
    bus = getattr(core, "event_bus", None)
    try:
        transport = durable_transport(bus)
    except InvalidArgument as exc:
        report.add("consumers", "transport", FAIL, str(exc))
        return
    if transport is None or not isinstance(bus, BaseEventBus):
        report.add(
            "consumers",
            "transport",
            SKIP,
            f"{len(consumers)} consumer(s); the event bus has no durable "
            "transport (EVENT_BUS_BACKEND=streams reports groups and lag)",
        )
        return
    if "inspection" not in bus.bus_features:
        report.add(
            "consumers",
            "transport",
            WARN,
            f"{len(consumers)} consumer(s); the event transport does not declare "
            "'inspection': consumer lag is not checked",
        )
        return
    group_info = transport.group_info
    infos: dict[str, list[Any]] = {}
    for channel, group, required in consumers:
        try:
            if channel not in infos:
                infos[channel] = list(
                    await asyncio.wait_for(group_info(channel), timeout=timeout)
                )
        except Exception as exc:  # noqa: BLE001 - reported
            report.add("consumers", f"{channel} -> {group}", FAIL, _error(exc))
            continue
        info = next((g for g in infos[channel] if g.group == group), None)
        name = f"{channel} -> {group}"
        if info is None:
            report.add(
                "consumers",
                name,
                WARN if required else SKIP,
                "group not created yet (the consumer has never subscribed)",
            )
            continue
        lag, pending = info.lag, info.pending
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

    # The loaded modules' configuration (their overrides, accepted risks and
    # refusals) is part of "config": it needs the composition too.
    needs_core = set(SECTIONS) - skip
    if not needs_core:
        return report
    if core_loader is None:
        core_loader = load_composition_core
    try:
        # Collect mode (ADR-0015 decision 8): a module's configuration refusal
        # is listed, every module's, instead of the first one ending the run.
        with _module_settings.collect_mode() as diagnosed:
            core = core_loader()
    except BaseException as exc:  # SystemExit from a bad module too
        if isinstance(exc, KeyboardInterrupt):
            raise
        if not diagnosed:
            report.add("modules", "composition_root", FAIL, _error(exc))
        else:
            _add_diagnosed(report, diagnosed)
        return report
    if diagnosed:
        # A partial composition would be reported as if it were the one the
        # deployment runs: stop at the refusals.
        _add_diagnosed(report, diagnosed)
        return report
    try:
        if "config" not in skip:
            check_module_settings(report, core)
        if "modules" not in skip:
            check_modules(report, core)
        if "infra" not in skip:
            await _guarded(report, "infra", lambda: check_infra(report, core, timeout))
        if "periodic" not in skip:
            await _guarded(
                report, "periodic", lambda: check_periodic(report, core, timeout)
            )
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
