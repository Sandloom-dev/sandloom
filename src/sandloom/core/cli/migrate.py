"""CLI entrypoint for core & per-module migrations (Phase I).

Usage examples (after installation):
  sandloom migrate plan
  sandloom migrate upgrade-core --apply
  sandloom migrate upgrade-module identity --apply
  sandloom migrate upgrade-all --apply [--lock-timeout 60] [--create-databases]
  sandloom migrate drift          # models vs chain heads on a scratch database
  sandloom migrate extract-module billing --from default --to postgresql://.../billing
  sandloom migrate extract-module billing --from default --to <url> --apply \
      --i-have-stopped-writes
  sandloom migrate extract-module billing --from default --to <url> --drop-source \
      --apply --i-have-stopped-writes

By default operations are dry-run unless --apply provided.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import sandloom.core.cli.migrate_drift as _migrate_drift
import sandloom.core.cli.migrate_rls as _migrate_rls
from sandloom.core.app_factory import enforce_configuration
from sandloom.core.cli.extract import (
    ExtractionError,
    ModuleExtractor,
    database_identity_of_url,
    module_data_spec,
    transfer_group_owners,
)
from sandloom.core.composition import COMPOSITION_ROOT_ENV as _COMPOSITION_ROOT_ENV
from sandloom.core.composition import (
    CompositionRootError,
    composition_root_name,
    load_composition_core,
)
from sandloom.core.config.settings import settings
from sandloom.core.descriptors import ModuleDescriptor
from sandloom.core.descriptors.module_names import module_name as _module_name
from sandloom.core.facade import CoreFacade  # facade used only for module descriptors
from sandloom.core.migrations.migrations import (
    _mask_url as _mask,
)
from sandloom.core.migrations.migrations import (
    build_plan,
    check_chain_collisions,
    ensure_core_infra_tables,
    ensure_database,
    list_database_instances,
    migration_lock,
    resolve_database_sync_url,
    revision_module,
    run_core_upgrade,
    upgrade_all_modules,
    upgrade_module,
)
from sandloom.core.migrations.urls import to_sync_url

#: Composition root whose module set the CLI migrates (``sandloom.main`` by default;
#: a downstream app may point it at its own ASGI module, e.g. ``myapp.asgi``).
#: Resolved by the shared :mod:`sandloom.core.composition` helper.
COMPOSITION_ROOT_ENV = _COMPOSITION_ROOT_ENV


def _load_core() -> CoreFacade:
    """Return the facade of the application the deploy actually runs.

    The CLI must migrate EXACTLY the module set ``create_app`` loads — including
    the installed modules ``sandloom.main`` composes when ``SANDLOOM_MODULES`` is
    unset, entry-point modules, and the same required/optional rules. A hand-rolled
    bare facade used to skip those defaults, so ``upgrade-all`` with
    ``SANDLOOM_MODULES`` unset applied only the core chain and exited 0. Building the
    composition root's app reuses that resolution verbatim (module loading
    performs no I/O; infrastructure is opened lazily).
    """
    core = load_composition_core()
    if not isinstance(core, CoreFacade):
        raise CompositionRootError(
            f"composition root {composition_root_name()!r} does not expose a "
            "CoreFacade; set SANDLOOM_COMPOSITION_ROOT to your ASGI module"
        )
    return core


def _find_descriptor(
    descriptors: Sequence[ModuleDescriptor], name: str
) -> ModuleDescriptor | None:
    """A loaded module by full dotted name or its last segment / chain key."""
    for d in descriptors:
        if d.name == name or _module_name(d.name) == name:
            return d
    for d in descriptors:
        if d.migrations_path and d.migration_chain_key() == name:
            return d
    return None


def _hosting_databases(descriptors: Sequence[ModuleDescriptor]) -> list[str]:
    """Databases that host a module (core chain targets), configured only."""
    configured = set(list_database_instances())
    hosting = {"default"} | {
        inst for d in descriptors for inst in d.database_instances()
    }
    unknown = sorted(hosting - configured)
    if unknown:
        raise RuntimeError(
            f"modules declare unconfigured database instance(s) {unknown} "
            f"(configured: {sorted(configured)}); add them to INFRA or fix "
            "ModuleDescriptor.migrations_database"
        )
    return sorted(hosting)


def _database_url(ref: str) -> str:
    """A configured instance name or a literal URL -> sync URL."""
    if "://" in ref:
        return to_sync_url(ref)
    return resolve_database_sync_url(ref)


def _module_consumer_groups(core: CoreFacade, module: str) -> list[str]:
    groups: list[str] = []
    for attr in ("event_consumers", "projection_consumers"):
        for descriptor in getattr(core, attr, None) or []:
            if getattr(descriptor, "module", None) == module:
                group = getattr(descriptor, "group_name", None)
                if group:
                    groups.append(str(group))
    return groups


def _extract_module(core: CoreFacade, args: argparse.Namespace) -> int:  # noqa: C901
    """``extract-module``: see :mod:`sandloom.core.cli.extract`."""
    target = _find_descriptor(core.module_descriptors, args.name)
    if target is None:
        print(
            f"Module not found: {args.name} (it must be LOADED by the composition "
            "root the extraction runs with — use the monolith's SANDLOOM_MODULES)",
            file=sys.stderr,
        )
        return 1
    owners: dict[str, str] = getattr(core, "_service_owners", None) or {}
    spec = module_data_spec(
        target,
        _module_consumer_groups(core, target.name),
        [name for name, owner in owners.items() if owner == target.name],
    )
    source_url, target_url = _database_url(args.source), _database_url(args.target)
    extractor = ModuleExtractor(
        spec, source_url, target_url, lock_timeout_ms=args.lock_timeout_ms
    )
    try:
        if args.verify:
            report = extractor.verify()
        elif args.drop_source:
            report = extractor.drop_source(
                writes_stopped=args.writes_stopped, apply=args.apply
            )
        elif args.unfence:
            report = extractor.unfence(apply=args.apply, force=args.force)
        elif args.apply:
            report = extractor.apply(writes_stopped=args.writes_stopped)
        else:
            report = extractor.plan()
    except ExtractionError as exc:
        print(f"extract-module refused/failed: {exc}", file=sys.stderr)
        return 1
    print(report.render())
    if report.problems:
        return 1
    plain = not (args.verify or args.drop_source or args.unfence)
    if plain and not args.no_transfer_groups:
        transport = getattr(getattr(core, "event_bus", None), "transport", None)
        source_id = database_identity_of_url(args.source_identity_url or source_url)
        target_id = database_identity_of_url(args.target_identity_url or target_url)

        async def _transfer() -> list[str]:
            try:
                return await transfer_group_owners(
                    transport,
                    target.name,
                    source_identity=source_id,
                    target_identity=target_id,
                    apply=args.apply,
                )
            finally:
                infra = getattr(core, "infra", None)
                if infra is not None:
                    await infra.aclose_all()

        moved = asyncio.run(_transfer()) if transport is not None else []
        verb = "transferred" if args.apply else "would transfer"
        for pair in moved:
            print(f"  streams group owner {verb}: {pair} -> {target_id}")
        if not moved:
            print("  streams group owners: none recorded for this module/database")
    return 0


def _positive_seconds(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {raw!r}") from exc
    if not 0 < value < float("inf"):
        raise argparse.ArgumentTypeError("must be a positive number of seconds")
    return value


def _add_lock_timeout(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--lock-timeout",
        type=_positive_seconds,
        default=None,
        metavar="SECONDS",
        help=(
            "Max wait for the per-database migration advisory lock held by "
            "another runner (default: MIGRATION_LOCK_TIMEOUT_SECONDS, 300). "
            "While waiting, the holder's pid / application_name / query start "
            "are logged."
        ),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser("sandloom migrate")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan", help="Show migration plan (dry-run)")
    p_plan.add_argument("--apply", action="store_true", help="(ignored for plan)")

    sub.add_parser("databases", help="List configured database instances")

    p_core = sub.add_parser("upgrade-core", help="Upgrade core Alembic migrations")
    p_core.add_argument("--rev", default="head", help="Target revision (default head)")
    p_core.add_argument(
        "--apply", action="store_true", help="Apply (otherwise dry-run)"
    )
    p_core.add_argument(
        "--database",
        default="default",
        help="Target database instance name (default: default)",
    )
    p_core.add_argument(
        "--all-databases",
        action="store_true",
        help="Apply the chain to every configured database",
    )
    _add_lock_timeout(p_core)

    p_outbox = sub.add_parser(
        "ensure-outbox",
        help=(
            "DEV/REPAIR ONLY: create the shared core infra tables "
            "(outbox_messages, audit_log, consumer_inbox, contract_command_log, "
            "http_idempotency_log) "
            "in a database. "
            "Production must use 'upgrade-core' (versioned, drift-aware)."
        ),
    )
    p_outbox.add_argument(
        "--database",
        default="default",
        help="Target database instance name (default: default)",
    )
    p_outbox.add_argument(
        "--all-databases",
        action="store_true",
        help="Ensure core infra tables on every configured database",
    )
    p_outbox.add_argument("--apply", action="store_true")
    p_outbox.add_argument(
        "--force",
        action="store_true",
        help="Allow --apply in production (otherwise refused; use upgrade-core)",
    )

    p_mod = sub.add_parser("upgrade-module", help="Upgrade single module migrations")
    p_mod.add_argument("name", help="Module name (e.g. sandloom_identity)")
    p_mod.add_argument("--rev", default="head")
    p_mod.add_argument("--apply", action="store_true")
    _add_lock_timeout(p_mod)

    p_rev = sub.add_parser(
        "revision",
        help=(
            "Create a new revision in ONE module's own chain (script_location ="
            " its migrations/, version table = alembic_version_<chain>, target ="
            " its database)"
        ),
    )
    p_rev.add_argument("name", help="Module name (e.g. billing or myapp.modules.x)")
    p_rev.add_argument("-m", "--message", required=True, help="Revision message")
    p_rev.add_argument(
        "--autogenerate",
        action="store_true",
        help="Diff the module's models against its database (must be at head)",
    )
    p_rev.add_argument(
        "--database",
        default=None,
        help="Database instance to compare against (only if the module has several)",
    )
    p_rev.add_argument("--rev-id", default=None, help="Explicit revision id")

    p_new = sub.add_parser(
        "new",
        help=(
            "Autogenerate a revision in ONE module's chain and add row-level "
            "security for each new TenantScoped table (spec §5.6). Compares "
            "against a scratch database with every chain at head (any state of "
            "your databases works; --against-database compares against the "
            "module's configured database, which must be at its head)"
        ),
    )
    p_new.add_argument("name", help="Module name (e.g. billing or myapp.modules.x)")
    p_new.add_argument("message", help="Revision message")
    p_new.add_argument(
        "--database",
        default=None,
        help="Database instance to compare against (only if the module has several)",
    )
    p_new.add_argument("--rev-id", default=None, help="Explicit revision id")
    p_new.add_argument(
        "--against-database",
        action="store_true",
        help="Compare against the module's configured database (at its head) "
        "instead of a scratch database",
    )

    sub.add_parser(
        "drift",
        help=(
            "Fail when a module's models differ from its chain head (SANDLOOM-T006): "
            "every chain is applied to a scratch database and compared"
        ),
    )

    p_all = sub.add_parser("upgrade-all", help="Upgrade all modules")
    # Kept only to give a clear error: a revision id belongs to ONE chain, so a
    # non-head target cannot be applied to the core chain and every module
    # chain at once. Use upgrade-core / upgrade-module for a specific revision.
    p_all.add_argument("--rev", default="head", help=argparse.SUPPRESS)
    p_all.add_argument("--apply", action="store_true")
    p_all.add_argument(
        "--create-databases",
        action="store_true",
        help="Create each hosting PostgreSQL database first when it is missing "
        "(the role needs CREATEDB; the microservices profile's one-shots: a "
        "service added to a stack whose volume already exists)",
    )
    _add_lock_timeout(p_all)
    _add_extract_parser(sub)
    return parser


def _add_extract_parser(sub: Any) -> None:
    p_ext = sub.add_parser(
        "extract-module",
        help=(
            "Move ONE module's existing data from a shared database into its "
            "own (maintenance window: the module's writers must be stopped)"
        ),
    )
    p_ext.add_argument("name", help="Module name (e.g. billing or sandloom_billing)")
    p_ext.add_argument(
        "--from",
        dest="source",
        required=True,
        help="Source database: a configured instance name or a URL",
    )
    p_ext.add_argument(
        "--to",
        dest="target",
        required=True,
        help="Target database: a configured instance name or a URL",
    )
    p_ext.add_argument("--apply", action="store_true", help="Execute (else dry-run)")
    p_ext.add_argument(
        "--i-have-stopped-writes",
        dest="writes_stopped",
        action="store_true",
        help="Confirm every writer of the module is stopped (required to apply)",
    )
    mode = p_ext.add_mutually_exclusive_group()
    mode.add_argument(
        "--verify",
        action="store_true",
        help="Only verify an extraction (row counts + checksums vs the manifest)",
    )
    mode.add_argument(
        "--drop-source",
        action="store_true",
        help="After a verified extraction: drop the module's tables from the source",
    )
    mode.add_argument(
        "--unfence",
        action="store_true",
        help="Roll back: make the source module tables writable again",
    )
    p_ext.add_argument(
        "--force",
        action="store_true",
        help="--unfence even after the extraction completed",
    )
    p_ext.add_argument(
        "--lock-timeout-ms",
        type=int,
        default=5000,
        help="How long the fence waits for table locks (default 5000)",
    )
    p_ext.add_argument(
        "--no-transfer-groups",
        action="store_true",
        help="Do not re-point the module's Streams consumer-group owners",
    )
    p_ext.add_argument(
        "--source-identity-url",
        default=None,
        help="URL the source services are configured with (group ownership), "
        "if it differs from --from",
    )
    p_ext.add_argument(
        "--target-identity-url",
        default=None,
        help="URL the new service will be configured with (group ownership), "
        "if it differs from --to",
    )


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - CLI wrapper
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    # The boot checks every entry point runs (ConfigurationError is a
    # SystemExit carrying the reason), also for the commands that never
    # build the application.
    enforce_configuration(settings)

    # Migration failures must produce a non-zero exit code: CI/CD treats 0
    # as "deploy may proceed". _load_core() runs load_modules(), which raises
    # ModuleLoadError when a required module fails — that must surface as a
    # clean exit 1 too, not an uncaught traceback, so it lives inside the try.
    if args.cmd == "upgrade-all" and args.rev != "head":
        print(
            "upgrade-all only migrates every chain to head: a revision id "
            "belongs to a single chain. Use 'upgrade-core --rev <rev>' or "
            "'upgrade-module <name> --rev <rev>'.",
            file=sys.stderr,
        )
        return 2

    try:
        core = _load_core()
        descriptors = core.module_descriptors
        check_chain_collisions(descriptors)
        command = _COMMANDS.get(args.cmd)
        if command is not None:
            return command(core, descriptors, args)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"Migration failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_plan(
    core: CoreFacade, descriptors: list[ModuleDescriptor], args: argparse.Namespace
) -> int:
    plan = build_plan(descriptors)
    for e in plan.entries:
        status = "valid" if e.valid else f"skip({e.reason})"
        print(f"{e.module}: {status} path={e.path}")
    # Show the database(s) each module is bound to (multi-DB visibility).
    for d in descriptors:
        dbs = ",".join(sorted(d.database_instances()))
        print(f"{d.name}: database={dbs}")
    return 0


def _cmd_databases(
    core: CoreFacade, descriptors: list[ModuleDescriptor], args: argparse.Namespace
) -> int:
    for name, url in sorted(list_database_instances().items()):
        print(f"{name}: {_mask(url)}")
    return 0


def _cmd_upgrade_core(
    core: CoreFacade, descriptors: list[ModuleDescriptor], args: argparse.Namespace
) -> int:
    targets = list(list_database_instances()) if args.all_databases else [args.database]
    urls = [resolve_database_sync_url(db_name) for db_name in targets]
    with migration_lock(urls if args.apply else [], timeout=args.lock_timeout):
        for url in urls:
            run_core_upgrade(head=args.rev, dry_run=not args.apply, database_url=url)
    return 0


def _cmd_ensure_outbox(
    core: CoreFacade, descriptors: list[ModuleDescriptor], args: argparse.Namespace
) -> int:
    # Production escape hatch is refused unless explicitly forced: the
    # sanctioned prod path is the versioned, drift-aware alembic chain.
    # checkfirst here would silently NOT repair an already-present but
    # outdated table, so it must not be the default prod provisioner.
    if args.apply and settings.is_prod and not args.force:
        print(
            "Refusing 'ensure-outbox --apply' in production: it cannot "
            "detect or fix schema drift. Use 'sandloom migrate upgrade-core "
            "--all-databases --apply' (versioned, drift-aware). Re-run "
            "with --force only to repair a database the chain cannot "
            "reach.",
            file=sys.stderr,
        )
        return 1
    # --force is a last-resort, non-migration-safe DDL repair. Even with
    # it, require an explicit env opt-in in prod so it can never become a
    # routine deploy step, and warn loudly that it does NOT reconcile
    # drift (checkfirst only checks existence).
    if args.apply and args.force:
        if settings.is_prod and not settings.accepts_risk("unversioned_ddl_repair"):
            print(
                "Refusing forced 'ensure-outbox --apply' in production "
                'without SANDLOOM_ACCEPTED_RISKS=["unversioned_ddl_repair"]. This '
                "path is NOT migration-safe (checkfirst does not reconcile "
                "schema drift); accept the risk only for a deliberate one-off "
                "repair, never as a deploy step.",
                file=sys.stderr,
            )
            return 1
        print(
            "WARNING: forced ensure-outbox is NOT migration-safe — it "
            "creates missing tables but does NOT reconcile drift in "
            "existing ones. Prefer 'upgrade-core'.",
            file=sys.stderr,
        )
    targets = list(list_database_instances()) if args.all_databases else [args.database]
    for db_name in targets:
        url = resolve_database_sync_url(db_name)
        ensure_core_infra_tables(url, dry_run=not args.apply)
    return 0


def _cmd_upgrade_module(
    core: CoreFacade, descriptors: list[ModuleDescriptor], args: argparse.Namespace
) -> int:
    target = _find_descriptor(descriptors, args.name)
    if not target:
        print(f"Module not found: {args.name}", file=sys.stderr)
        return 1
    urls = [resolve_database_sync_url(db) for db in target.database_instances()]
    with migration_lock(urls if args.apply else [], timeout=args.lock_timeout):
        upgrade_module(target, revision=args.rev, dry_run=not args.apply)
    return 0


def _cmd_revision(
    core: CoreFacade, descriptors: list[ModuleDescriptor], args: argparse.Namespace
) -> int:
    target = _find_descriptor(descriptors, args.name)
    if target is None:
        print(
            f"Module not found: {args.name} (loaded: "
            f"{sorted(d.name for d in descriptors)}; check SANDLOOM_MODULES / "
            "SANDLOOM_COMPOSITION_ROOT)",
            file=sys.stderr,
        )
        return 1
    for created in revision_module(
        target,
        args.message,
        autogenerate=args.autogenerate,
        database=args.database,
        rev_id=args.rev_id,
    ):
        print(f"Created {created}")
    return 0


def _cmd_new(
    core: CoreFacade, descriptors: list[ModuleDescriptor], args: argparse.Namespace
) -> int:
    """``sandloom migrate new <module> "<message>"`` (autogenerate + RLS)."""
    target = _find_descriptor(descriptors, args.name)
    if target is None:
        print(
            f"Module not found: {args.name} (loaded: "
            f"{sorted(d.name for d in descriptors)})",
            file=sys.stderr,
        )
        return 1
    tables = _migrate_rls.tenant_tables(target.name)
    if args.against_database or _migrate_drift.drift_supported() is not None:
        created_files = revision_module(
            target,
            args.message,
            autogenerate=True,
            database=args.database,
            rev_id=args.rev_id,
        )
    else:
        # A scratch database on the default server with every chain at head:
        # the comparison sees exactly the chain, whatever state (or absence)
        # the configured databases are in.
        with _migrate_drift.chains_at_head(descriptors) as url:
            created_files = revision_module(
                target,
                args.message,
                autogenerate=True,
                database=args.database,
                rev_id=args.rev_id,
                url=url,
            )
    for created in created_files:
        covered = _migrate_rls.add_tenant_rls(Path(created), tables)
        print(f"Created {created}")
        for table in covered:
            print(f"  row-level security: {table}")
    return 0


def _cmd_upgrade_all(
    core: CoreFacade, descriptors: list[ModuleDescriptor], args: argparse.Namespace
) -> int:
    # Full per-(module|database) orchestration:
    # 1. The CORE chain (outbox/audit/inbox/command log) goes to EVERY
    #    database that hosts a module — each database owns its own
    #    transactional outbox. ``default`` is always a host.
    # The whole run holds a per-database advisory lock so concurrent
    # migrate jobs serialise instead of racing DDL / version rows.
    hosting = _hosting_databases(descriptors)
    urls = {db: resolve_database_sync_url(db) for db in hosting}
    if args.create_databases:
        for db_name, url in urls.items():
            if not args.apply:
                print(f"would create database {db_name} ({_mask(url)}) if missing")
            elif ensure_database(url):
                print(f"created database {db_name} ({_mask(url)})")
    with migration_lock(
        list(urls.values()) if args.apply else [],
        timeout=args.lock_timeout,
    ):
        for db_name in hosting:
            run_core_upgrade(
                head="head",
                dry_run=not args.apply,
                database_url=urls[db_name],
            )
        # 2. Each module's OWN chain goes to the ONE database hosting
        #    its tables (ModuleDescriptor.database_instances()).
        upgrade_all_modules(descriptors, revision="head", dry_run=not args.apply)
    return 0


#: sub-command -> handler ``(core, descriptors, args) -> exit code``; each runs
#: inside main's CLI boundary (an exception exits 1).
_COMMANDS: dict[
    str, Callable[[CoreFacade, list[ModuleDescriptor], argparse.Namespace], int]
] = {
    "plan": _cmd_plan,
    "databases": _cmd_databases,
    "upgrade-core": _cmd_upgrade_core,
    "ensure-outbox": _cmd_ensure_outbox,
    "upgrade-module": _cmd_upgrade_module,
    "revision": _cmd_revision,
    "new": _cmd_new,
    "drift": lambda _core, descriptors, _args: _migrate_drift.run(descriptors),
    "extract-module": lambda core, _descriptors, args: _extract_module(core, args),
    "upgrade-all": _cmd_upgrade_all,
}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
