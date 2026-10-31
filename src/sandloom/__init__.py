"""Sandloom framework — top-level CLI (`sandloom`).

Subcommands:
    sandloom new <app>        scaffold a standalone application that consumes the
                         framework (see sandloom.core.cli.project)
    sandloom add ...          generate code: a module, an event, a contract, a
                         consumer, a saga, a projection, a task, and a
                         module's microservices deployment (sandloom add deploy)
                         (see sandloom.core.cli.add)
    sandloom check            run the architecture (module isolation) rules against
                         ./architecture_rules.yml (see sandloom.core.cli.check)
    sandloom explain <code>   the long form of a framework error code (SANDLOOM-xxxx)
    sandloom docs <name>      the Public API 1.0 reference of a name or module
                         (sandloom.sdk; see sandloom.sdk.tooling.docs_main)
    sandloom generate [module ...]
                         write every generated file (module, route and
                         contract snapshots, contract stubs, DTO copies,
                         exported event schemas) after the compatibility
                         checks (see sandloom.core.cli.generated)
    sandloom test [module]    the module's tests, the fast (--full: whole) parallel
                         suite and every static gate, with one summary; it
                         never writes a generated file (see
                         sandloom.core.cli.run_tests)
    sandloom migrate ...      database migrations (see sandloom.core.cli.migrate);
                         `sandloom migrate new <module> "<msg>"` autogenerates a
                         revision with row-level security
    sandloom projection ...   operate query projections (see
    sandloom.core.cli.projections)
    sandloom outbox ...       list / requeue dead-lettered outbox events
    sandloom streams ...      list / replay the Streams dead-letter queue
                         (both: see sandloom.core.cli.events)
    sandloom keys generate    generate an asymmetric signing key pair (JWT
                         access tokens, or --purpose rpc: a service identity)
    sandloom keys allowlist   derive RPC_ALLOWED_CALLS from SERVICE_BINDINGS
                         (both: see sandloom.core.cli.keys)
    sandloom contracts ...    sync-contract snapshots + rolling-upgrade compatibility
                         checks (see sandloom.core.cli.contracts)
    sandloom routes ...       per-module route snapshots (method, path, operation
                         class) and the route reference (sandloom.core.cli.routes)
    sandloom admin export     export the platform-owner UI manifest
    sandloom backup ...       backup plan, restore verification, security-cache
                         reconciliation after a restore (see sandloom.core.cli.backup)
    sandloom sagas ...        list / show / retry / park / resolve saga instances
                         (see sandloom.core.cli.sagas)
    sandloom security ...     incident response: mass session revocation, forced
                         password reset, audit export (see sandloom.core.cli.security)
    sandloom config dump      the effective settings, secrets redacted, with the
                         source of each value (see sandloom.core.cli.config)
    sandloom doctor           read-only deployment preflight: config, modules,
                         infra reachability, migration heads, consumer lag,
                         RLS (see sandloom.core.cli.doctor)
    sandloom version          print the installed framework version
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
from typing import Any


def _static_import(name: str) -> Any:
    """Import a STATIC CLI module (``sandloom new`` / ``add`` / ``check``).

    ``sandloom.core`` resolves its re-exports lazily, so these code-generation and
    static-analysis commands never build the settings object and work in an
    empty directory with no DATABASE_URL / JWT_SECRET configured.
    """
    return importlib.import_module(name)


#: Commands that read the deployment's configuration: they refuse to run on
#: one a process would refuse to boot with, like the app and the worker.
#: (``migrate`` checks in its own entry point, ``sandloom.core.cli.migrate``; ``doctor``
#: reports the problems instead.)
_CONFIGURED_COMMANDS = frozenset(
    {
        "projection",
        "outbox",
        "streams",
        "contracts",
        "routes",
        "backup",
        "security",
        "config",
        "sagas",
    }
)


def _load_test_env_for_authoring(*, create_database: bool) -> None:
    """``sandloom migrate new`` and ``sandloom generate`` run where ``sandloom
    test`` runs: with the project's ``.env.test`` (+ ``SANDLOOM_TEST_*``)
    loaded, like the test harness, before the settings object is built.
    ``migrate new`` writes only to a scratch database on that server, never
    to the configured ones; ``generate`` connects to none."""
    try:
        fixtures = _static_import("sandloom.core.testing.env")
    except ImportError:  # no test tooling installed: the process env stands
        return
    if fixtures.find_env_file([Path.cwd()]) is not None:
        fixtures.load_test_env([Path.cwd()], create_database=create_database)


def _enforce_configuration() -> None:
    """The boot checks of ``create_app`` (``SystemExit`` with the reason)."""
    # Imported here: the static commands (new / add / check / keys /
    # admin) must not build the settings object.
    from sandloom.core.app_factory import enforce_configuration

    enforce_configuration()


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("sandloom")
    except Exception:  # pragma: no cover  # noqa: BLE001 - editable / unbuilt tree has no dist metadata
        return "0.0.0+local"


def _cmd_new(argv: list[str]) -> int:
    project = _static_import("sandloom.core.cli.project")
    generate_project, quickstart = project.generate_project, project.quickstart

    parser = argparse.ArgumentParser(
        prog="sandloom new", description="Scaffold a standalone Sandloom application"
    )
    parser.add_argument("app", help="application name (snake_case, e.g. helpdesk)")
    parser.add_argument(
        "--dir", type=Path, default=Path.cwd(), help="parent directory (default: cwd)"
    )
    parser.add_argument(
        "--with",
        dest="batteries",
        default="",
        help="comma-separated standard modules to install and load: "
        "identity,tenancy,billing (the sandloom-<module> packages)",
    )
    parser.add_argument(
        "--no-batteries",
        action="store_true",
        help="install no standard module (minimal app)",
    )
    parser.add_argument(
        "--module",
        default="example",
        help="name of the initial sample module (default: example)",
    )
    parser.add_argument(
        "--framework-req",
        default=None,
        help=(
            "override the framework dependency requirement string "
            "(default: sandloom[redis,celery]>=0.7.0a4,<0.8; each --with module "
            "adds its sandloom-<module> package)"
        ),
    )
    parser.add_argument(
        "--framework-source",
        choices=("pypi", "git", "path"),
        default=None,
        help="distribution source (checkout: git; installed wheel: pypi)",
    )
    parser.add_argument(
        "--framework-path",
        type=Path,
        default=None,
        help=(
            "framework repository (or its backend/ directory, holding "
            "packages/) for --framework-source path"
        ),
    )
    args = parser.parse_args(argv)

    batteries: list[str] = []
    if not args.no_batteries and args.batteries:
        batteries = [m.strip() for m in args.batteries.split(",") if m.strip()]

    try:
        created = generate_project(
            args.app,
            args.dir,
            with_batteries=batteries,
            sample_module=args.module,
            framework_req=args.framework_req,
            framework_source=args.framework_source,
            framework_path=args.framework_path,
        )
    except (ValueError, FileExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    root = args.dir / args.app
    print(f"Created application '{args.app}' ({len(created)} files) at {root}")
    # The generated files (snapshots, schemas, stubs): the app's own CI gate
    # (`sandloom test --ci`) only checks them, so a fresh app must carry them.
    problem = project.write_generated_files(root)
    if problem is not None:
        print(
            f"note: generated files not written ({problem}); "
            "run `uv run sandloom generate` once the app installs"
        )
    # The quickstart's one source (sandloom.core.cli.project.QUICKSTART): the docs
    # are tested against it and the CI job `quickstart` runs these lines.
    in_cwd = args.dir.resolve() == Path.cwd().resolve()
    lines = quickstart(args.app, args.module, path=None if in_cwd else str(root))
    print("\nNext steps:\n" + "".join(f"  {line}\n" for line in lines))
    return 0


def main() -> None:  # noqa: C901
    parser = argparse.ArgumentParser(
        prog="sandloom", description="Sandloom framework CLI"
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("new", add_help=False, help="scaffold a standalone application")
    sub.add_parser(
        "add",
        add_help=False,
        help="generate code: module, event, contract, consumer, saga, "
        "projection, task, deploy",
    )
    sub.add_parser(
        "check", add_help=False, help="run architecture (module isolation) checks"
    )
    sub.add_parser(
        "explain", add_help=False, help="explain an SANDLOOM-xxxx error code"
    )
    sub.add_parser(
        "docs", add_help=False, help="the sandloom.sdk reference: sandloom docs <name>"
    )
    sub.add_parser(
        "generate",
        add_help=False,
        help="write the generated files (snapshots, stubs, DTO copies, schemas)",
    )
    sub.add_parser(
        "test", add_help=False, help="module tests + fast suite + static gates"
    )
    sub.add_parser(
        "context",
        add_help=False,
        help="compact module map with interfaces and file pointers",
    )
    sub.add_parser(
        "graph",
        add_help=False,
        help="offline declared architecture graph and module discovery",
    )
    sub.add_parser(
        "explore", add_help=False, help="generate and open the architecture explorer"
    )
    sub.add_parser("migrate", add_help=False, help="run database migrations")
    sub.add_parser("projection", add_help=False, help="operate query projections")
    sub.add_parser("outbox", add_help=False, help="requeue dead-lettered outbox events")
    sub.add_parser(
        "streams", add_help=False, help="replay the Streams dead-letter queue"
    )
    sub.add_parser("keys", add_help=False, help="generate signing keys")
    sub.add_parser(
        "contracts",
        add_help=False,
        help="contract snapshots / compatibility checks",
    )
    sub.add_parser(
        "routes", add_help=False, help="route snapshots (each route's policy)"
    )
    sub.add_parser("admin", add_help=False, help="export a generated admin UI manifest")
    sub.add_parser("backup", add_help=False, help="backup plan / restore verification")
    sub.add_parser(
        "security", add_help=False, help="incident response (sessions, audit export)"
    )
    sub.add_parser(
        "sagas", add_help=False, help="list / show / retry / park / resolve sagas"
    )
    sub.add_parser(
        "config", add_help=False, help="effective settings (sandloom config dump)"
    )
    sub.add_parser("doctor", add_help=False, help="read-only deployment preflight")
    sub.add_parser("version", help="print the framework version")

    args, rest = parser.parse_known_args()
    if args.command in _CONFIGURED_COMMANDS:
        _enforce_configuration()

    if args.command == "new":
        raise SystemExit(_cmd_new(rest))
    if args.command == "add":
        raise SystemExit(_static_import("sandloom.core.cli.add").main(rest))
    if args.command == "check":
        raise SystemExit(_static_import("sandloom.core.cli.check").main(rest))
    if args.command == "generate":
        _load_test_env_for_authoring(create_database=False)
        raise SystemExit(_static_import("sandloom.core.cli.generated").main(rest))
    if args.command == "explore":
        raise SystemExit(_static_import("sandloom.core.cli.explore").main(rest))
    if args.command in {"context", "graph"}:
        raise SystemExit(
            _static_import("sandloom.core.cli.inspection.cli").main(args.command, rest)
        )
    if args.command == "test":
        raise SystemExit(_static_import("sandloom.core.cli.run_tests").main(rest))
    if args.command == "docs":
        raise SystemExit(_static_import("sandloom.sdk.tooling").docs_main(rest))
    if args.command == "explain":
        raise SystemExit(_static_import("sandloom.core.cli.explain").main(rest))
    if args.command == "migrate":
        if rest[:1] == ["new"]:
            _load_test_env_for_authoring(create_database=True)
        from sandloom.core.cli.migrate import main as migrate_main

        raise SystemExit(migrate_main(rest))
    if args.command == "projection":
        from sandloom.core.cli.projections import main as projection_main

        raise SystemExit(projection_main(rest))
    if args.command == "outbox":
        from sandloom.core.cli.events import outbox_main

        raise SystemExit(outbox_main(rest))
    if args.command == "streams":
        from sandloom.core.cli.events import streams_main

        raise SystemExit(streams_main(rest))
    if args.command == "keys":
        raise SystemExit(_static_import("sandloom.core.cli.keys").main(rest))
    if args.command == "contracts":
        from sandloom.core.cli.contracts import main as contracts_main

        raise SystemExit(contracts_main(rest))
    if args.command == "routes":
        from sandloom.core.cli.routes import main as routes_main

        raise SystemExit(routes_main(rest))
    if args.command == "admin":
        raise SystemExit(_static_import("sandloom.core.cli.admin").main(rest))
    if args.command == "backup":
        from sandloom.core.cli.backup import main as backup_main

        raise SystemExit(backup_main(rest))
    if args.command == "security":
        from sandloom.core.cli.security import main as security_main

        raise SystemExit(security_main(rest))
    if args.command == "sagas":
        raise SystemExit(_static_import("sandloom.core.cli.sagas").main(rest))
    if args.command == "config":
        from sandloom.core.cli.config import main as config_main

        raise SystemExit(config_main(rest))
    if args.command == "doctor":
        from sandloom.core.cli.doctor import main as doctor_main

        raise SystemExit(doctor_main(rest))
    if args.command == "version":
        print(_version())
        raise SystemExit(0)

    parser.print_help()
    raise SystemExit(0)


if __name__ == "__main__":  # pragma: no cover
    main()
