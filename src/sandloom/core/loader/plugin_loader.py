from __future__ import annotations

import dataclasses
import importlib
import logging
import os
import types
from collections.abc import Iterable, Sequence
from importlib import metadata
from typing import TYPE_CHECKING, Any

import sandloom.core.observability.guardrails as _guardrails
from sandloom.core.descriptors import (
    ContractRequirement,
    FailedModule,
    ModuleDescriptor,
    ModuleLoadSummary,
    SkippedModule,
)

try:  # optional metrics (Phase G)
    from sandloom.core.observability import metrics as _metrics_mod
except Exception:  # pragma: no cover  # noqa: BLE001 - metrics are optional
    _metrics_mod = None  # type: ignore
from sandloom.core.config.settings import settings
from sandloom.core.descriptors.module_names import (
    ENTRY_POINT_GROUP,
    installed_modules,
    module_name,
    resolve_module_names,
)
from sandloom.core.loader.manifest import module_manifest
from sandloom.core.module_api import ModuleAPI
from sandloom.core.module_api._registration import (
    _RegistrationControlFlow,
    pending_saga_requirements,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.core.facade import CoreFacade

logger = logging.getLogger("sandloom.core.loader.plugin_loader")


class ModuleRegistrationError(RuntimeError):
    """Module is importable but does not expose register(core) -> descriptor."""


class ModuleDependencyError(RuntimeError):
    """A module's declared ``depends_on`` dependency failed to load."""


class _DeferModule(_RegistrationControlFlow):
    """Internal signal: a dependency listed later has not been attempted yet.

    Raised inside the registration transaction (so nothing the module buffered
    is applied); the module is retried after the rest of the list.
    """


class _DuplicateModule(_RegistrationControlFlow):
    """Internal signal: a registered module name is already loaded.

    Raised inside the registration transaction so the duplicate's register()
    side effects are rolled back, then handled as a (non-fatal) duplicate.
    """

    def __init__(self, module_name: str) -> None:
        super().__init__(module_name)
        self.module_name = module_name


class ModuleLoadError(RuntimeError):
    """Raised when required module(s) fail to load (aggregated)."""

    def __init__(self, summary: ModuleLoadSummary) -> None:
        failed_required = [f for f in summary.failed if f.required]
        msg = (
            "Required modules failed: "
            + "; ".join(
                f"{f.name}: {_guardrails.scrub_text(f.error)}" for f in failed_required
            )
            + f" (total_failed={len(summary.failed)})"
        )
        super().__init__(msg)
        self.summary = summary


class ContractBindingError(RuntimeError):
    """A loaded module consumes a REQUIRED contract nobody provides or binds.

    Raised at startup (after every module registered, so a provider loaded
    later in the list counts): the contract is neither provided by a module in
    this process nor bound in ``SERVICE_BINDINGS``. Without this check an
    incomplete binding set only fails at the first request that needs it.
    """

    def __init__(self, missing: list[tuple[str, str, str]]) -> None:
        self.missing = missing
        parts = [
            f"{module} consumes {contract!r}" + (f" ({reason})" if reason else "")
            for module, contract, reason in missing
        ]
        super().__init__(
            "Consumed contracts are neither provided locally nor bound in "
            "SERVICE_BINDINGS: " + "; ".join(parts)
        )


def _preflight_consumes(
    core: CoreFacade, summary: ModuleLoadSummary, *, enforce: bool = True
) -> None:
    """Check every loaded module's ``consumes`` against providers + bindings.

    Records the resolution of each consumed contract (``local`` / ``remote``
    / ``missing``) on ``summary.consumes`` (served by ``/diagnostics/core``),
    warns for a missing optional contract and raises
    :class:`ContractBindingError` for missing required ones. No I/O: a remote
    binding's reachability is a readiness concern, not a load-time one.

    ``enforce=False`` is the topology read (:func:`load_module_topology`):
    the resolutions are recorded, nothing is raised or logged, because the
    caller is about to generate the very bindings that are missing.
    """
    bindings = getattr(core.settings, "service_bindings", None) or {}
    missing: list[tuple[str, str, str]] = []
    for desc in summary.loaded:
        entries: list[dict[str, Any]] = []
        for raw in getattr(desc, "consumes", None) or []:
            req = ContractRequirement(raw) if isinstance(raw, str) else raw
            if core.service_registry.contains(req.name):
                resolution = "local"
            elif req.name in bindings:
                resolution = "remote"
            else:
                resolution = "missing"
            entries.append(
                {
                    "contract": req.name,
                    "required": req.required,
                    "resolution": resolution,
                }
            )
            if resolution != "missing" or not enforce:
                continue
            if req.required:
                missing.append((desc.name, req.name, req.reason))
            else:
                logger.warning(
                    "[plugin_loader] %s consumes optional contract %r, which is "
                    "neither provided locally nor bound in SERVICE_BINDINGS%s",
                    desc.name,
                    req.name,
                    f" ({req.reason})" if req.reason else "",
                )
        if entries:
            summary.consumes[desc.name] = entries
    if missing:
        raise ContractBindingError(missing)


def apply_module_defaults(
    default_modules: Sequence[str] = (), default_optional: Sequence[str] = ()
) -> None:
    """Fill the module lists a composition root ships as defaults.

    The single module-resolution rule shared by ``create_app`` and every CLI
    that must see the same module set (``sandloom-migrate``): when the deployment
    configured no ``SANDLOOM_MODULES`` / ``MODULE_REQUIRED``, the composition root's
    defaults apply. Mutates the settings singleton so the loader and readiness
    agree.

    Required-module rule when ``MODULE_REQUIRED`` is empty: EVERY requested
    module is required — in every profile, whether ``SANDLOOM_MODULES`` was
    configured or filled from the composition root's defaults. A module that
    fails to load aborts startup instead of silently disappearing (its routes
    would answer 404 and its tests would go green against nothing, SANDLOOM-B001).
    Optionality is explicit: the root's ``default_optional`` (only for a
    defaulted list), ``define_module(..., optional=True)``, or a
    ``MODULE_REQUIRED`` naming the subset that must load.
    """
    global _defaulted_modules
    explicit = bool(settings.sandloom_modules) and (
        settings.sandloom_modules is not _defaulted_modules
    )
    if not settings.sandloom_modules:
        _defaulted_modules = list(default_modules)
        settings.sandloom_modules = _defaulted_modules
    if not settings.module_required:
        optional = set() if explicit else set(default_optional)
        settings.module_required = [
            m for m in settings.sandloom_modules if m not in optional
        ]


#: The list object ``apply_module_defaults`` filled SANDLOOM_MODULES with, so a
#: second composition (tests, CLIs) does not mistake defaults for an explicit
#: deployment choice.
_defaulted_modules: list[str] | None = None


def _fail_fast() -> bool:
    """MODULE_FAIL_FAST: abort on the FIRST failure of ANY module.

    One rule for both load paths (explicit list and entry points): with
    fail-fast on, any module failure — required or optional — raises
    immediately. With it off, failures are aggregated and only REQUIRED
    failures abort startup (``ModuleLoadError`` at the end of the pass).
    """
    return bool(getattr(settings, "module_fail_fast", False))


def _matches(dep: str, name: str) -> bool:
    """A ``depends_on`` entry names a module by import path or module name."""
    return dep == name or module_name(name) == dep or name.rsplit(".", 1)[-1] == dep


def _check_dependencies(
    desc: ModuleDescriptor, summary: ModuleLoadSummary, pending: Sequence[str]
) -> None:
    """Fail (or defer) a module whose dependency did not load.

    * a dependency that FAILED in this pass -> the dependent fails too (its
      failure is required/optional per the dependent's own rules);
    * a dependency still pending later in the list -> defer and retry;
    * a dependency not requested in this process at all -> allowed: modules
      talk via events / service contracts, so a microservice may deploy a
      dependent without its dependency (``depends_on`` then only orders
      migrations).
    """
    for dep in desc.depends_on or []:
        if any(_matches(dep, d.name) for d in summary.loaded):
            continue
        failed = [f.name for f in summary.failed if _matches(dep, f.name)]
        if failed:
            raise ModuleDependencyError(
                f"module {desc.name!r} depends on {dep!r}, which failed to load"
            )
        if any(_matches(dep, p) for p in pending):
            raise _DeferModule(dep)


def _check_chain_collision(desc: ModuleDescriptor, summary: ModuleLoadSummary) -> None:
    """Two loaded modules must not share one migration chain key."""
    if not desc.migrations_path:
        return
    key = desc.migration_chain_key()
    for other in summary.loaded:
        if other.migrations_path and other.migration_chain_key() == key:
            raise ModuleRegistrationError(
                f"modules {other.name!r} and {desc.name!r} both map to "
                f"migration chain {key!r} (alembic_version_{key}, table prefix "
                f"'{key}_'); rename one package or set a distinct "
                "ModuleDescriptor.migrations_chain on it"
            )


def _check_descriptor_name(
    name: str, desc: ModuleDescriptor, summary: ModuleLoadSummary
) -> None:
    """Refuse a listed module whose descriptor name another module already took.

    Everything downstream (owners, databases, migrations, ``loaded_modules``)
    is keyed by the descriptor name: two packages sharing one would shadow
    each other. An explicitly listed module is never skipped silently — the
    collision is a load failure (fatal when the module is required).
    """
    if any(d.name == desc.name for d in summary.loaded):
        raise ModuleRegistrationError(
            f"module {name!r} registers descriptor name {desc.name!r}, which an "
            "already loaded module uses; descriptor names must be unique"
        )


def _is_required(name: str, desc: ModuleDescriptor | None) -> bool:
    """Required = listed in MODULE_REQUIRED (filled by apply_module_defaults:
    every requested module unless optional) and not ``optional=True`` itself.

    A module that fails on import has no descriptor, so the settings list is
    what decides: required by default, optional only by explicit choice.
    """
    if desc is not None and desc.optional:
        return False
    return name in _configured_paths("module_required")


def _configured_paths(setting: str) -> set[str]:
    """A module-list setting as import paths (entries may be module names)."""
    entries = [str(e) for e in getattr(settings, setting, []) or []]
    return set(entries) | set(resolve_module_names(entries))


def _should_skip(name: str) -> bool:
    wl = _configured_paths("module_whitelist")
    bl = _configured_paths("module_blacklist")
    if wl and name not in wl:
        return True
    return name in bl


def _invoke_register(module: Any, api: ModuleAPI) -> ModuleDescriptor:
    register_fn = getattr(module, "register", None)
    if not callable(register_fn):
        # Importable but unregistrable is a defect, not a soft skip: a
        # required module would otherwise be silently absent at runtime.
        raise ModuleRegistrationError(
            f"Module '{getattr(module, '__name__', '?')}' has no callable "
            "register(core) entry point"
        )
    # ``api`` is the NARROW, draft-backed ModuleAPI — the module sees only the
    # registration surface, and its writes are buffered until commit.
    result = register_fn(api)
    if isinstance(result, ModuleDescriptor):
        # Phase I: auto-detect migrations directory if not provided
        if result.migrations_path is None:
            try:
                mod_file = getattr(module, "__file__", None)
                if mod_file:
                    candidate = os.path.join(os.path.dirname(mod_file), "migrations")
                    if os.path.isdir(candidate):
                        result.migrations_path = candidate
            except Exception:  # pragma: no cover  # noqa: BLE001, S110 - migrations dir detection is best effort
                pass
        return result
    raise ModuleRegistrationError(
        f"Module '{getattr(module, '__name__', '?')}' register(core) "
        "must return a ModuleDescriptor"
    )


def _record_manifest(
    summary: ModuleLoadSummary, desc: ModuleDescriptor, api: ModuleAPI, module: Any
) -> None:
    """Record the loaded module's manifest entry (spec §5.7; no I/O)."""
    attr = "__name__" if isinstance(module, types.ModuleType) else "__module__"
    package = getattr(module, attr, None)
    summary.manifests[desc.name] = module_manifest(
        desc,
        getattr(api, "_draft", None),
        package=package if isinstance(package, str) else None,
    )


def _merge_saga_requirements(desc: ModuleDescriptor, api: ModuleAPI) -> None:
    """Add the contracts the module's sagas call to its ``consumes`` (ADR-0008),
    merged BY NAME (one entry per contract):

    * a contract the module declared as a plain name or with ``methods=()``
      already covers every method: the saga's entry adds nothing;
    * otherwise the methods are unioned (an empty set means all, and wins),
      ``required`` is OR-ed and the declared reason is kept.
    """
    index: dict[str, int] = {}
    for i, entry in enumerate(desc.consumes):
        name = entry if isinstance(entry, str) else entry.name
        index.setdefault(name, i)
    for req in pending_saga_requirements(api):
        at = index.get(req.name)
        if at is None:
            index[req.name] = len(desc.consumes)
            desc.consumes.append(req)
            continue
        declared = desc.consumes[at]
        if isinstance(declared, str) or not declared.methods:
            continue  # the declared entry covers every method already
        methods = (
            ()
            if not req.methods
            else tuple(dict.fromkeys((*declared.methods, *req.methods)))
        )
        desc.consumes[at] = dataclasses.replace(
            declared,
            required=declared.required or req.required,
            methods=methods,
            reason=declared.reason or req.reason,
        )


def _preflight_capabilities(core: CoreFacade, desc: ModuleDescriptor) -> None:
    """Verify every capability a module declares is available before serving.

    Resolves each :class:`CapabilityRequirement` through the infrastructure
    registry (no I/O — providers build lazily; feature checks are in-memory). A
    ``required`` requirement that cannot be satisfied raises a diagnostic
    :class:`CapabilityError`, which — inside the registration transaction —
    rolls back the module's registration and (if the module is required) aborts
    startup. Optional requirements only warn.

    Skipped entirely when no infrastructure registry is bound (bare facades in
    unit tests), so test apps need no infra wiring.
    """
    # Ambiguous ownership must fail at load, not at the first contract call.
    desc.database_instances()
    registry = getattr(core, "_infra", None)
    if registry is None:
        return

    for req in desc.effective_capabilities():
        try:
            # Build-free validation: capability spec, instance, driver and
            # declared features are checked WITHOUT instantiating the provider,
            # so a plugin's factory cannot run network/connection code here.
            registry.validate_requirement(
                req.capability,
                req.instance,
                features=req.features or None,
                version=req.version,
            )
        except Exception as exc:  # optional reqs must not abort
            # A required capability is fatal (rolls back the module's
            # registration; aborts startup if the module is required). An
            # optional one (search/vector/workflow a module can live without)
            # only warns — even on an unexpected driver-metadata error.
            if req.required:
                raise
            logger.warning(
                "[plugin_loader] Optional capability %s/%s unavailable for %s: %s",
                req.capability,
                req.instance,
                desc.name,
                exc,
            )


def _record_failure(
    name: str, desc: ModuleDescriptor | None, exc: Exception, summary: ModuleLoadSummary
) -> None:
    logger.error("[plugin_loader] Failed module %s: %s", name, exc, exc_info=exc)

    summary.failed.append(
        FailedModule(
            name=name,
            error=_guardrails.scrub_text(str(exc)),
            required=_is_required(name, desc),
        )
    )
    if _metrics_mod is not None:
        try:
            if hasattr(_metrics_mod, "module_load_failures_total"):
                _metrics_mod.module_load_failures_total.inc()
        except Exception:  # pragma: no cover  # noqa: BLE001, S110 - metrics are optional
            pass


def _load_list(  # noqa: C901
    module_names: Sequence[str], core: CoreFacade, summary: ModuleLoadSummary
) -> None:
    """Import and register each module in the requested order.

    Modules load in ``SANDLOOM_MODULES`` order. ``depends_on`` is honoured at load
    time as far as it can be without importing ahead: a module whose dependency
    FAILED fails too (see :func:`_check_dependencies`), and one whose dependency
    is listed later is deferred (its registration is discarded and retried
    after the rest of the list). A dependency cycle among pending modules fails
    every module in it.

    Invariant this relies on: a module's ``register()`` must NOT resolve sibling
    services (``core.get_service``) during registration — load order makes no
    guarantee that a sibling provider is registered yet. Sync registry contracts
    are resolved lazily at request time, by which point all modules are loaded.
    """
    queue: list[str] = list(module_names)
    deferred_rounds: dict[str, int] = {}
    while queue:
        name = queue.pop(0)
        if _should_skip(name):
            summary.skipped.append(SkippedModule(name=name, reason="list"))
            logger.info("[plugin_loader] Skipped by list rules: %s", name)
            continue
        if any(d.name == name for d in summary.loaded):
            summary.duplicates.append(name)
            summary.skipped.append(SkippedModule(name=name, reason="duplicate"))
            continue
        desc: ModuleDescriptor | None = None
        try:
            # Registration is transactional: everything the module registers is
            # buffered in a draft and applied atomically on success; a failure
            # commits nothing (services/tasks/health-checks/routes/schemas).
            with core.module_registration(name) as api:
                mod = importlib.import_module(name)
                desc = _invoke_register(mod, api)
                if desc:
                    _merge_saga_requirements(desc, api)
                    _check_descriptor_name(name, desc, summary)
                    pending = [n for n in queue if not _should_skip(n)]
                    _check_dependencies(desc, summary, pending)
                    _check_chain_collision(desc, summary)
                    _preflight_capabilities(core, desc)
                if desc and desc.routes:
                    api.include_router(desc.routes)
            if desc:
                summary.loaded.append(desc)
                _record_manifest(summary, desc, api, mod)
                logger.info("[plugin_loader] Loaded %s v%s", desc.name, desc.version)
        except _DeferModule as wait_for:
            # Retry after the rest of the list; a module deferred more times
            # than there are modules left is stuck in a cycle.
            rounds = deferred_rounds.get(name, 0) + 1
            deferred_rounds[name] = rounds
            if rounds > len(queue) + 1:
                exc = ModuleDependencyError(
                    f"module {name!r} depends on {wait_for.args[0]!r}, which "
                    "could not be loaded first (dependency cycle?)"
                )
                _record_failure(name, desc, exc, summary)
                if _fail_fast():
                    raise exc from None
                continue
            queue.append(name)
        except Exception as e:
            _record_failure(name, desc, e, summary)
            if _fail_fast():
                raise


def _load_entry_points(core: CoreFacade, summary: ModuleLoadSummary) -> None:  # noqa: C901
    try:
        group: Iterable[Any] = tuple(
            metadata.entry_points().select(group=ENTRY_POINT_GROUP)
        )
    except Exception:  # pragma: no cover
        logger.exception("[plugin_loader] Entry points enumeration failed")
        return
    for ep in group:
        value = getattr(ep, "value", "")
        if isinstance(value, str) and value:
            core._known_module_packages.add(value.split(":", 1)[0])
        ep_name = getattr(ep, "name", None)
        if ep_name and _should_skip(ep_name):
            summary.skipped.append(SkippedModule(name=ep_name, reason="list"))
            continue
        desc: ModuleDescriptor | None = None
        ep_label = str(getattr(ep, "name", "?"))
        try:
            # Transactional: a duplicate or a failure commits nothing the entry
            # point's register() buffered (services/tasks/health/routes/schemas).
            with core.module_registration(ep_label) as api:
                loaded = ep.load()
                package = getattr(loaded, "__name__", None)
                if isinstance(package, str):
                    core._known_module_packages.add(package)
                desc = _invoke_register(loaded, api)
                if desc and any(d.name == desc.name for d in summary.loaded):
                    raise _DuplicateModule(desc.name)
                if desc:
                    # Entry points load after the explicit list: nothing is
                    # pending, so a dependency either loaded, failed or is
                    # absent from this process.
                    _check_dependencies(desc, summary, ())
                    _check_chain_collision(desc, summary)
                    _preflight_capabilities(core, desc)
                if desc and desc.routes:
                    api.include_router(desc.routes)
            if desc:
                summary.loaded.append(desc)
                _record_manifest(summary, desc, api, loaded)
                logger.info(
                    "[plugin_loader] Loaded entrypoint %s v%s", desc.name, desc.version
                )
        except _DuplicateModule as dup:
            summary.duplicates.append(dup.module_name)
            summary.skipped.append(
                SkippedModule(name=dup.module_name, reason="duplicate")
            )
        except Exception as e:
            _record_failure(ep_label, desc, e, summary)
            # Same rule as _load_list (see _fail_fast): any failure aborts when
            # MODULE_FAIL_FAST is on; otherwise only required failures abort,
            # aggregated at the end of the pass.
            if _fail_fast():
                raise


def load_modules(
    core_facade: CoreFacade, modules: Sequence[str] | None = None
) -> list[ModuleDescriptor]:
    summary = load_modules_with_summary(core_facade, modules)
    return summary.loaded


def load_modules_with_summary(
    core_facade: CoreFacade, modules: Sequence[str] | None = None
) -> ModuleLoadSummary:
    """Load the modules and enforce what they consume (the application path).

    A required consumed contract that is neither provided by a loaded module
    nor bound in ``SERVICE_BINDINGS`` raises :class:`ContractBindingError`.
    """
    return _load_with_summary(core_facade, modules, enforce_bindings=True)


def load_module_topology(
    core_facade: CoreFacade, modules: Sequence[str]
) -> ModuleLoadSummary:
    """Load modules only to read their topology: routes, provided contracts
    and the resolution of what they consume.

    For deploy tooling that *derives* ``SERVICE_BINDINGS`` and gateway routes
    (``sandloom add deploy``, the deploy-profile checks): a module loaded on
    its own consumes contracts that are not bound yet, by definition, so the
    consumes preflight records ``missing`` instead of raising. Never use it to
    build an application: ``create_app`` loads with
    :func:`load_modules_with_summary`, which stays strict.
    """
    return _load_with_summary(core_facade, modules, enforce_bindings=False)


def _load_with_summary(
    core_facade: CoreFacade,
    modules: Sequence[str] | None,
    *,
    enforce_bindings: bool,
) -> ModuleLoadSummary:
    # Entries name a module by import path or by its `sandloom.modules`
    # entry-point name; an ambiguous name is refused before anything loads
    # (SANDLOOM-B020).
    explicit = resolve_module_names(
        list(modules)
        if modules is not None
        else list(getattr(settings, "sandloom_modules", []))
    )
    if getattr(settings, "module_load_entrypoints", False):
        # Auto-discovered providers are requested too. Validate the complete
        # set before either explicit or automatic registration can run.
        resolve_module_names(
            [
                *explicit,
                *(m.path for m in installed_modules() if not _should_skip(m.name)),
            ]
        )
    summary = ModuleLoadSummary(explicit=explicit)
    core_facade._known_module_packages.update(explicit)
    if getattr(settings, "module_load_entrypoints", False):
        # Declare every entry-point package before ANY provider registers a
        # contract. Otherwise an explicit module could expose a DTO belonging
        # to an entry point that has not been loaded yet.
        try:
            for entry in metadata.entry_points().select(group=ENTRY_POINT_GROUP):
                value = getattr(entry, "value", "")
                if isinstance(value, str) and value:
                    core_facade._known_module_packages.add(value.split(":", 1)[0])
        except Exception:  # pragma: no cover - reported again by the loader
            logger.exception("[plugin_loader] Entry point package discovery failed")
    logger.info("[plugin_loader] Start load; explicit=%s", explicit)
    _load_list(explicit, core_facade, summary)
    if getattr(settings, "module_load_entrypoints", False):
        _load_entry_points(core_facade, summary)
    core_facade.loaded_modules = [d.name for d in summary.loaded]
    core_facade.module_descriptors = list(summary.loaded)
    logger.info(
        "[plugin_loader] Done. loaded=%d failed=%d skipped=%d duplicates=%d success=%s",
        len(summary.loaded),
        len(summary.failed),
        len(summary.skipped),
        len(summary.duplicates),
        summary.success(),
    )
    # Aggregated error handling (Phase D): raise only after full processing
    if any(f.required for f in summary.failed):
        # Only raise aggregated error; early raises already handled for fail_fast
        raise ModuleLoadError(summary)
    # Every module is registered now: check what each one consumes.
    _preflight_consumes(core_facade, summary, enforce=enforce_bindings)
    return summary
