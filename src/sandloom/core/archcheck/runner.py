"""Run every architecture rule over a tree described by an :class:`ArchRules`.

This is what ``sandloom check`` executes and what ``tools/checks/check_architecture.py``
uses for the framework's own manifest check. Each rule maps to one analyzer in
:mod:`sandloom.core.archcheck.analyzers`; the framework's pytest architecture suite
calls the same analyzers per file.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

import sandloom.core.archcheck.analyzers as az
import sandloom.core.archcheck.api10_rules as a10
import sandloom.core.archcheck.module_rules as mr
from sandloom.core import error_codes
from sandloom.core.archcheck.manifest import ArchRules
from sandloom.core.descriptors.module_names import installed_modules, module_name

_LINE_PREFIX = re.compile(r"^Line (\d+): ")


@dataclass(frozen=True)
class Violation:
    """One rule hit. Analyzers prefix ``detail`` with ``Line N:``; the
    formatted form puts the line into a clickable ``path:N`` location."""

    rule: str
    file: Path
    detail: str
    #: ``error`` fails ``sandloom check``; ``warning`` and ``info`` are reported.
    severity: str = "error"

    @property
    def line(self) -> int | None:
        match = _LINE_PREFIX.match(self.detail)
        return int(match.group(1)) if match else None

    @property
    def message(self) -> str:
        return _LINE_PREFIX.sub("", self.detail, count=1)

    @property
    def code(self) -> str | None:
        entry = error_codes.lookup(self.rule)
        return entry.code if entry else None

    def location(self, root: Path | None = None) -> str:
        shown = self.file
        if root is not None:
            try:
                shown = self.file.relative_to(root)
            except ValueError:
                pass
        line = self.line
        return f"{shown.as_posix()}:{line}" if line else shown.as_posix()

    def format(self, root: Path | None = None) -> str:
        """``path:N: error[SANDLOOM-Cnnn rule]: message`` (or
        ``warning`` / ``info``).
        """
        tag = f"{self.code} {self.rule}" if self.code else self.rule
        return f"{self.location(root)}: {self.severity}[{tag}]: {self.message}"


#: Rule id -> one-line rationale + fix (printed by ``sandloom check``).
RULES: dict[str, str] = {
    "module-boundaries": (
        "a business module imports a sibling module — talk through versioned "
        "events (publish_event / register_event_consumer) or a service-registry "
        "contract (core.get_service), never a direct import"
    ),
    "framework-imports": (
        "import outside the framework public API — use sandloom.core.{di, facade, "
        "module_api, descriptors, orm, projections} and sandloom.shared.* only"
    ),
    "raw-provider": (
        "provider escape hatch (raw_client/raw_backend/raw) — use the portable "
        "capability surface (cache.get/set, taskqueue.enqueue, ...)"
    ),
    "infra-imports": (
        "infra-internal module, legacy global or raw backend SDK imported — use "
        "DbSession / Database(name) / di.cache() / publish_event / Capability"
    ),
    "kernel-handles": (
        "kernel handle reached by attribute (core.event_bus / core.infra / "
        "core.service_registry / app.state.core) — publish_event, "
        "register_event_consumer, core.cache()/database()/capability() and "
        "core.get_service(...) are the module surface"
    ),
    "uow-boundary": (
        "db.commit()/db.rollback() in module code — the UoW owns the "
        "transaction; use flush() and begin_nested()"
    ),
    "best-effort-effects": (
        "publish_event / record_audit downgraded to best-effort (required=False: "
        "a direct bus publish with no outbox, or a skipped audit row) without a "
        "`# sandloom: best-effort <reason>` marker — drop required=False and run the "
        "call inside a Unit of Work, or justify the downgrade on the call"
    ),
    "get-core-await": (
        "direct get_core(request) call not awaited — get_core is async since "
        "0.6.0: `await get_core(request)`, or the sync "
        "`core_from_request(request)`; Depends(get_core) is unchanged"
    ),
    "endpoints-outside-api": (
        "HTTP endpoint outside the module's api/ layer — move it into api/"
    ),
    "orm-leakage": (
        "ORM model / SQLAlchemy in an endpoint file — map to pydantic DTOs in "
        "services and keep persistence out of api/"
    ),
    "schema-orm-imports": (
        "schemas/ imports ORM models or SQLAlchemy — keep DTOs persistence-free"
    ),
    "contract-dtos": (
        "a contract signature names a DTO of the module's own package that has "
        "no generated copy in the shared DTO package (callers import the copy) "
        "— run `sandloom generate`"
    ),
    "test-imports": (
        "module test imports outside its whitelist (public API, sandloom.shared, "
        "the test API sandloom.core.testing / contract_testing, its own package) — "
        "a kernel internal or ANOTHER business module; use sandloom.core.testing: "
        "the plugin fixtures (client, db, make_headers, ...), "
        "make_user_headers, bind_test_uow, sandloom.core.orm.Base, and other "
        "modules' contracts / events instead of their code"
    ),
    "tenant-scoped-models": (
        "an ORM model / Core Table has a tenant-like column (tenant_id, "
        "organization_id, org_id, workspace_id) but does not inherit "
        "TenantScoped — the ORM tenant guard never filters it, so one "
        "forgotten where(organization_id == ...) leaks across tenants; "
        "declare it `class X(TenantScoped, Base)` (drop the own column) and "
        "ship enable_tenant_rls(op, <table>), or list a deliberate global "
        "table under tenant_scope_exempt in architecture_rules.yml with a reason"
    ),
    "unsafe-tenant-sql": (
        "runtime text(), exec_driver_sql or Core __table__ access can bypass "
        "the tenant ORM guard — use ORM entities, or explicit unscoped() "
        "for trusted system work (PostgreSQL RLS remains enforced)"
    ),
    "event-schema-channels": (
        "publish_event names a channel the module registers no schema for — "
        "add it with its JSON schema to the module's events.EVENT_SCHEMAS"
    ),
    "guard-order": (
        "an idempotency_guard / tenant-dependent require(...) gate is declared "
        "before the authentication / TenantScope() it needs — declare it after "
        "them (decorator dependencies run before endpoint parameters)"
    ),
    "consumed-contracts": (
        "get_service names a contract the module does not declare — add "
        'ContractRequirement("<name>", required=...) to define_module(..., '
        "consumes=[...])"
    ),
    "db-dependency-scope": (
        'Depends(get_db...) without scope="function" commits after the response '
        '— declare scope="function" or use DbSession from sandloom.core.di'
    ),
}

#: The Public API 1.0 checks (:mod:`sandloom.core.archcheck.api10_rules`).
API10_RULES: dict[str, str] = {
    "module-local-imports": (
        "(warning until the bundled modules are clean) a function-local "
        "import in a business module — import at module level; a genuinely "
        "optional dependency keeps its local import with "
        "`# sandloom: optional-import <reason>`; typing-only imports go under "
        "`if TYPE_CHECKING:`"
    ),
    "untagged-sensitive": (
        "(warning) an effect that moves money or changes credentials/sessions "
        'carries no risk tag — @command(..., risk="money") or '
        '@sensitive_effect("admin") or @sensitive_effect("security") at the source; '
        "a write route whose "
        "authority check is hand-rolled declares sensitive(risk) in dependencies"
    ),
    "multi-store-waivers": (
        "(info) every allow_multi_db_write opt-out with its reason, for review; "
        "a waiver without a reason is an error"
    ),
    "unsafe-tenant-query": (
        "a raw non-relational store query outside `with unscoped():` reads "
        "every tenant — use the store's tenant-scoped operations"
    ),
    "event-view-compat": (
        "an event view is incompatible with its producer (or unverified: "
        "warning), or a view calls .publish() — only the producer publishes"
    ),
    "contract-union-hint": (
        "(info) an ambiguous union in a contract signature uses codec 1; a "
        "discriminated type lets codec 2 apply"
    ),
    "account-route-mismatch": (
        "a route with an organization parameter gates billing with "
        'of=Account.user() — use Account.org(param=...) or Account.user(reason="...")'
    ),
}


def _planned(rule: str) -> bool:
    entry = error_codes.lookup(rule)
    return entry is not None and entry.planned is not None


#: A check whose catalogue code is still ``planned`` is PENDING: implemented
#: and tested (``run_checks(..., pending=True)``), not yet enforced by
#: ``sandloom check``. Clearing ``planned`` for the code activates it.
PENDING_RULES: dict[str, str] = {k: v for k, v in API10_RULES.items() if _planned(k)}
RULES.update({k: v for k, v in API10_RULES.items() if k not in PENDING_RULES})

#: Inline waiver: ``# sandloom: allow <rule-id|code> <reason>`` on the reported line.
_WAIVER = re.compile(r"#\s*sandloom:\s*allow\s+(\S+)\s+\S")


def iter_module_test_files(rules: ArchRules, module: str) -> Iterator[Path]:
    """Every ``*.py`` under a ``tests`` directory of ``module``."""
    for py in sorted((rules.modules_path / module).rglob("*.py")):
        parts = py.relative_to(rules.modules_path).parts
        if any(p.startswith(".") for p in parts) or "__pycache__" in parts:
            continue
        if "tests" in parts:
            yield py


def check_test_imports(rules: ArchRules, module: str) -> list[Violation]:
    """``test-imports``: module tests import only their whitelist.

    The whitelist is ``public_api`` + ``shared_api`` + ``test_api`` + the
    module's own package; any other import from ``sandloom`` or the application's
    modules package (a kernel internal, a sibling business module) is a
    violation. Honours the manifest ``allowlist`` (file globs relative to the
    manifest), e.g. the framework's bundled modules' legacy test debt.
    """
    own = rules.module_package(module)
    allowed = (*rules.public_api, *rules.shared_api, *rules.test_api)
    namespaces = (
        "sandloom",
        *([rules.modules_package] if rules.modules_package else []),
        *(rules.module_package(m) for m in rules.business_modules()),
        *installed_module_packages(rules),
    )
    violations: list[Violation] = []
    for py in iter_module_test_files(rules, module):
        rel = py.relative_to(rules.root) if rules.root in py.parents else py
        try:
            hits = az.module_test_import_hits(
                py,
                own_package=own,
                allowed=allowed,
                namespaces=namespaces,
                source_root=rules.source_root,
            )
        except SyntaxError as exc:
            violations.append(Violation("test-imports", py, f"syntax error: {exc}"))
            continue
        reported: set[tuple[int, str]] = set()
        for line, imported, text in hits:
            if _is_allowed(rel, imported, rules.allowlist) or (line, text) in reported:
                continue
            reported.add((line, text))
            violations.append(Violation("test-imports", py, f"Line {line}: {text}"))
    return violations


def check_tenant_scoped_models(rules: ArchRules) -> list[Violation]:
    """``tenant-scoped-models``: a table with ``tenant_id`` is ``TenantScoped``.

    Scans every non-test, non-migration file of every business module. A hit
    whose table is listed in the manifest's ``tenant_scope_exempt`` is
    accepted; an exemption that matches no hit is reported as stale (the
    ratchet only shrinks).
    """
    violations: list[Violation] = []
    used: set[str] = set()
    for module in rules.business_modules():
        for py in iter_module_files(rules, module, skip_migrations=True):
            try:
                hits = az.tenant_column_hits(py)
            except SyntaxError as exc:
                violations.append(
                    Violation("tenant-scoped-models", py, f"syntax error: {exc}")
                )
                continue
            for line, name, table, column in hits:
                if table is not None and table in rules.tenant_scope_exempt:
                    used.add(table)
                    continue
                shown = f" (table {table})" if table else ""
                violations.append(
                    Violation(
                        "tenant-scoped-models",
                        py,
                        f"Line {line}: {name}{shown} declares {column} but "
                        "does not inherit TenantScoped (per-tenant rows "
                        "without the tenant guard)",
                    )
                )
    manifest = rules.root / "architecture_rules.yml"
    for table in sorted(set(rules.tenant_scope_exempt) - used):
        violations.append(  # noqa: PERF401 - cold path (startup / diagnostics)
            Violation(
                "tenant-scoped-models",
                manifest,
                f"stale tenant_scope_exempt entry {table!r}: no model with "
                "a tenant-like column declares that table any more — remove "
                "the entry",
            )
        )
    return violations


def _skip(path: Path, *, migrations: bool) -> bool:
    parts = path.parts
    if any(p.startswith(".") for p in parts):
        return True
    if "tests" in parts or "__pycache__" in parts:
        return True
    return migrations and "migrations" in parts


def iter_module_files(
    rules: ArchRules, module: str, *, skip_migrations: bool = False
) -> Iterator[Path]:
    for py in sorted((rules.modules_path / module).rglob("*.py")):
        if not _skip(py.relative_to(rules.modules_path), migrations=skip_migrations):
            yield py


def iter_schema_files(rules: ArchRules, module: str) -> Iterator[Path]:
    """A module's DTO layer: every file of ``schemas/`` or a single ``schemas.py``."""
    base = rules.modules_path / module
    if (base / "schemas.py").is_file():
        yield base / "schemas.py"
    if (base / "schemas").is_dir():
        yield from sorted((base / "schemas").rglob("*.py"))


def _matches_any(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def _is_allowed(rel: Path, imported: str, allowlist: list[dict[str, Any]]) -> bool:
    rel_posix = rel.as_posix()
    for entry in allowlist:
        file_pat = entry.get("file")
        imports = [str(i) for i in entry.get("imports") or []]
        if (
            file_pat
            and fnmatch.fnmatch(rel_posix, str(file_pat))
            and _matches_any(imported, imports)
        ):
            return True
    return False


def _covered(imp: str, prefixes: Iterable[str]) -> bool:
    return any(imp == p or imp.startswith(p + ".") for p in prefixes)


def installed_module_packages(rules: ArchRules) -> list[str]:
    """Import paths of the installed ``sandloom.modules`` packages that are
    not this tree's own modules: other modules, never importable."""
    own = {rules.module_package(m) for m in rules.business_modules()}
    return sorted({m.path for m in installed_modules()} - own)


def _installed_module_violation(
    imp: str, packages: Iterable[str], file: Path, line: int
) -> Violation | None:
    package = next((p for p in packages if _covered(imp, [p])), None)
    if package is None:
        return None
    return Violation(
        "module-boundaries",
        file,
        f"Line {line}: {imp} (installed module package {package!r}: another "
        "module — talk to it through its events and contracts, never an import)",
    )


def installed_driver_modules() -> list[str]:
    """The modules installed ``sandloom.drivers`` entry points point at. A module
    asks for a capability and never constructs its driver; a package's separate
    ``contracts`` module stays importable."""
    entries = metadata.entry_points().select(group="sandloom.drivers")
    return sorted({entry.value.partition(":")[0] for entry in entries})


def driver_import_violations(
    file: Path, rules: ArchRules, drivers: Iterable[str]
) -> list[str]:
    """Imports of an installed driver: from such a package a module may import only
    ``<package>.contracts``. Any other part of it, the bare package included, can reach
    the driver by attribute. An application's own package stays importable: only
    the driver's module is refused there."""
    drivers = list(drivers)
    own_top = rules.modules_package.partition(".")[0]
    packages = {d.partition(".")[0] for d in drivers} - {own_top}
    tree = az.parse_file(file)
    refs = az.import_refs(tree, list(file.relative_to(rules.source_root).parts[:-1]))
    found = []
    for line, imported, _text in refs:
        top = imported.partition(".")[0]
        if _covered(imported, [f"{top}.contracts"]):
            continue
        if _covered(imported, drivers) or top in packages:
            found.append(
                f"Line {line}: {imported} can reach a driver: ask for the capability "
                f"instead (a module may import {top}.contracts)"
            )
    return found


def check_manifest_imports(rules: ArchRules) -> list[Violation]:  # noqa: C901
    """Public-API, forbidden-pattern and (optional) kernel layering checks.

    Scans every non-test, non-migration file under ``modules_path`` (the
    kernel package only for layering). The framework tree reports
    ``sandloom.core.*`` imports outside ``public_api``; an application additionally
    reports any other ``sandloom.*`` import that is neither public API nor
    ``shared_api`` (e.g. a bundled module such as ``sandloom_identity``).
    """
    violations: list[Violation] = []
    own_prefix = rules.modules_package
    installed = installed_module_packages(rules)
    for file in sorted(rules.modules_path.rglob("*.py")):
        rel_mod = file.relative_to(rules.modules_path)
        rel = file.relative_to(rules.root) if rules.root in file.parents else file
        in_kernel = bool(rules.kernel_package) and (
            rel_mod.parts[0] == rules.kernel_package
        )
        if any(p.startswith(".") for p in rel_mod.parts):
            continue
        # One report per imported module (``from m import a, b`` yields m.a, m.b).
        imports = list(
            dict.fromkeys(az.parse_imports(file, source_root=rules.source_root))
        )
        try:
            tree = az.parse_file(file)
        except SyntaxError as exc:
            violations.append(
                Violation("framework-imports", file, f"syntax error: {exc}")
            )
            continue
        import_lines = {
            imported: line
            for line, imported, _text in az.import_refs(
                tree,
                list(file.relative_to(rules.source_root).parts[:-1]),
            )
        }
        if in_kernel:
            if rules.layers_enabled:
                violations += _layer_violations(rules, file, rel, imports)
            continue
        if rel_mod.parts[0] in rules.framework_packages:
            continue  # framework code above the kernel: import-linter's job
        if _skip(rel_mod, migrations=True):
            continue
        for imp in imports:
            found = _installed_module_violation(
                imp, installed, file, import_lines.get(imp, 1)
            )
            if found is not None and not _is_allowed(rel, imp, rules.allowlist):
                violations.append(found)
            if imp == "sandloom.core" or imp.startswith("sandloom.core."):
                if not _covered(imp, rules.public_api) and not _is_allowed(
                    rel, imp, rules.allowlist
                ):
                    violations.append(
                        Violation(
                            "framework-imports",
                            file,
                            f"Line {import_lines.get(imp, 1)}: {imp} "
                            "(non_public_api: not in public_api)",
                        )
                    )
            elif (
                not rules.is_framework_tree
                and imp.startswith("sandloom.")
                and not _covered(imp, rules.shared_api)
                and not _covered(imp, rules.public_api)
                and not (own_prefix and _covered(imp, [own_prefix]))
                and not _is_allowed(rel, imp, rules.allowlist)
            ):
                violations.append(
                    Violation(
                        "framework-imports",
                        file,
                        f"Line {import_lines.get(imp, 1)}: {imp} "
                        "(framework_module: only the public API and "
                        f"{', '.join(sorted(rules.shared_api))} may be imported)",
                    )
                )
            if (
                rules.forbidden_patterns
                and _matches_any(imp, rules.forbidden_patterns)
                and not _is_allowed(rel, imp, rules.allowlist)
            ):
                violations.append(
                    Violation(
                        "framework-imports",
                        file,
                        f"Line {import_lines.get(imp, 1)}: {imp} (forbidden_pattern)",
                    )
                )
    return violations


def _layer_of(module: str, defs: dict[str, set[str]]) -> str | None:
    for lname, inc in defs.items():
        if any(module == i or module.startswith(i + ".") for i in inc):
            return lname
    return None


def _layer_violations(
    rules: ArchRules, file: Path, rel: Path, imports: list[str]
) -> list[Violation]:
    out: list[Violation] = []
    rel_src = file.relative_to(rules.source_root).with_suffix("")
    mod_name = ".".join(rel_src.parts)
    own = _layer_of(mod_name, rules.layers_defs)
    for imp in imports:
        target = _layer_of(imp, rules.layers_defs)
        if not (own and target):
            continue
        allowed: set[str] = set()
        for frm, to in rules.layer_rules:
            if frm == own:
                allowed |= to
        if target not in allowed:
            out.append(
                Violation("framework-imports", file, f"{imp} (layer {own} -> {target})")
            )
    return out


PerFile = Callable[[Path], list[str]]


def _per_module_rules(rules: ArchRules, module: str) -> dict[str, PerFile]:
    modules = rules.business_modules()
    package = rules.modules_package
    roots = rules.model_roots
    drivers = installed_driver_modules()
    return {
        "module-boundaries": lambda p: az.find_cross_module_imports(
            p, module, modules, modules_package=package
        ),
        "raw-provider": az.find_raw_provider_calls,
        "unsafe-tenant-sql": lambda p: (
            []
            if "migrations" in p.relative_to(rules.modules_path / module).parts
            else az.find_unsafe_tenant_sql(p)
        ),
        "infra-imports": lambda p: (
            az.find_legacy_infra_imports(p)
            + driver_import_violations(p, rules, drivers)
        ),
        "kernel-handles": az.find_kernel_handle_access,
        "uow-boundary": az.find_uow_boundary_breaks,
        "best-effort-effects": az.find_unmarked_best_effort_effects,
        "get-core-await": az.find_unawaited_get_core,
        "endpoints-outside-api": lambda p: az.find_endpoints_outside_api(
            p, module_root=rules.modules_path / module
        ),
        "orm-leakage": lambda p: az.find_orm_leakage(p, model_roots=roots),
        "contract-dtos": lambda p: az.find_contract_dto_leaks(
            p,
            rules.module_package(module),
            source_root=rules.source_root,
            generated=rules.shared_dir
            / "contracts"
            / f"{module_name(rules.module_package(module))}_dto.py",
        ),
    }


def run_checks(  # noqa: C901
    rules: ArchRules, *, only: Iterable[str] | None = None, pending: bool = False
) -> list[Violation]:
    """Run the selected rules (default: all) and return every violation.

    ``pending=True`` also offers the :data:`PENDING_RULES` (tests, previews).
    """
    known = {**RULES, **PENDING_RULES} if pending else RULES
    selected = set(only) if only is not None else set(known)
    unknown = selected - set(known)
    if unknown:
        raise ValueError(
            f"unknown rule(s) {sorted(unknown)}; choose from {sorted(known)}"
        )
    violations: list[Violation] = []
    if "framework-imports" in selected:
        violations += check_manifest_imports(rules)
    if "tenant-scoped-models" in selected:
        violations += check_tenant_scoped_models(rules)
    violations += _api10_tree_checks(rules, selected)
    for module in rules.business_modules():
        violations += _module_level_checks(rules, module, selected)
        per_file = {
            k: v for k, v in _per_module_rules(rules, module).items() if k in selected
        }
        for py in iter_module_files(rules, module):
            for rule, analyzer in per_file.items():
                try:
                    found = analyzer(py)
                except SyntaxError as exc:
                    violations.append(Violation(rule, py, f"syntax error: {exc}"))
                    continue
                violations += [Violation(rule, py, d) for d in found]
        if "test-imports" in selected:
            violations += check_test_imports(rules, module)
        if "schema-orm-imports" in selected:
            for py in iter_schema_files(rules, module):
                violations += [
                    Violation("schema-orm-imports", py, d)
                    for d in az.find_schema_orm_imports(py)
                ]
    return [v for v in violations if not _waived(v)]


def _api10_tree_checks(rules: ArchRules, selected: set[str]) -> list[Violation]:
    """The Public API 1.0 checks over every business module (spec §5, §7.3)."""
    out: list[Violation] = []
    analyzers = {k: v for k, v in a10.per_file_rules().items() if k in selected}
    modules: dict[str, list[Path]] = {}
    for module in rules.business_modules():
        files = list(iter_module_files(rules, module, skip_migrations=True))
        modules[module] = files
        for py in files:
            for rule, analyzer in analyzers.items():
                try:
                    found = analyzer(py)
                except SyntaxError as exc:
                    out.append(Violation(rule, py, f"syntax error: {exc}"))
                    continue
                severity = a10.SEVERITY[rule]
                out += [Violation(rule, py, d, severity) for d in found]
            if "multi-store-waivers" in selected:
                out += [
                    Violation("multi-store-waivers", py, d, level)
                    for level, d in a10.find_multi_store_waivers(py)
                ]
    if "event-view-compat" in selected:
        exported = rules.shared_dir / "events"
        out += [
            Violation("event-view-compat", py, d, level)
            for level, py, d in a10.find_event_view_problems(
                modules, exported if exported.is_dir() else None
            )
        ]
    return out


def _module_level_checks(
    rules: ArchRules, module: str, selected: set[str]
) -> list[Violation]:
    """The rules of :mod:`sandloom.core.archcheck.module_rules` for one module."""
    files = list(iter_module_files(rules, module, skip_migrations=True))
    module_dir = rules.modules_path / module
    out: list[Violation] = []
    checks: dict[str, Callable[[], list[tuple[Path, str]]]] = {
        "event-schema-channels": lambda: mr.find_unregistered_event_channels(
            module_dir, files
        ),
        "guard-order": lambda: mr.find_guard_order_problems(files),
        "consumed-contracts": lambda: mr.find_undeclared_contracts(files),
    }
    for rule, check in checks.items():
        if rule not in selected:
            continue
        try:
            out += [Violation(rule, py, detail) for py, detail in check()]
        except SyntaxError as exc:
            out.append(Violation(rule, module_dir, f"syntax error: {exc}"))
    if "db-dependency-scope" in selected:
        for py in files:
            try:
                found = mr.find_unscoped_db_dependencies(py)
            except SyntaxError:
                continue
            out += [Violation("db-dependency-scope", py, d) for d in found]
    return out


def _waived(violation: Violation) -> bool:
    """A ``# sandloom: allow <rule|code> <reason>`` comment on the reported line."""
    line = violation.line
    if line is None or not violation.file.is_file():
        return False
    try:
        text = az.read_source(violation.file).splitlines()[line - 1]
    except (IndexError, OSError):
        return False
    match = _WAIVER.search(text)
    if match is None:
        return False
    return match.group(1) in {violation.rule, violation.code}


__all__ = [
    "API10_RULES",
    "PENDING_RULES",
    "RULES",
    "Violation",
    "check_manifest_imports",
    "check_tenant_scoped_models",
    "check_test_imports",
    "iter_module_files",
    "iter_module_test_files",
    "iter_schema_files",
    "run_checks",
]
