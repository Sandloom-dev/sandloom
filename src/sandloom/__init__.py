"""Sandloom framework — top-level CLI (`sandloom`).

Subcommands:
    sandloom new [<app>]      scaffold a standalone application that consumes the
                         framework; at a terminal it asks what the flags
                         leave open, --yes asks nothing (see
                         sandloom.core.cli.wizard and sandloom.core.cli.project)
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
                         checks (see sandloom.core.cli.generated);
                         `--new-major <name>` copies a released contract or
                         event to its next major beside it; `--draft <name>`
                         writes a contract, event or module as a draft
    sandloom release          generate, and mark every contract and event of
                         this project released: from then on a breaking change
                         of one is a new major (ADR-0017)
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
    sandloom admin ...        platform administrators (create / invite / list)
                         and the admin UI manifest (export)
    sandloom backup ...       backup plan, restore verification, security-store
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
    sandloom worker [check]   run a task worker for the loaded modules; `check` is the
                         container healthcheck (see sandloom.core.cli.worker)
    sandloom beat             run the scheduler, one per broker and module set
                         (see sandloom.core.cli.worker)
    sandloom dev ...          local Postgres + Redis per repository, a database
                         per checkout: up / status / run / reset / down /
                         prune / logs (see sandloom.core.cli.dev)
    sandloom version          print the installed framework version

A command that cannot reach a backing service prints one line naming
`sandloom dev up` (SANDLOOM-R014); `--debug` or SANDLOOM_DEBUG=1 shows the
traceback instead, and the framework's DEBUG startup lines (LOG_LEVEL=DEBUG
unless LOG_LEVEL is exported).
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
    """``sandloom new``: the wizard at a terminal, the flags elsewhere
    (:mod:`sandloom.core.cli.wizard`)."""
    wizard = _static_import("sandloom.core.cli.wizard")
    return int(wizard.main(argv, version=_version()))


#: Commands that connect to the configured database / Redis: they warn when
#: an exported URL differs from the `.env` block `sandloom dev up` wrote.
_CONNECTING_COMMANDS = _CONFIGURED_COMMANDS | {"migrate", "doctor"}


#: Commands that read the files of the app whose stack lines ``sandloom new``
#: may have left as tokens (a bare kernel): they say what to run instead of
#: failing on a compose file or an ``INFRA`` that does not parse.
_NEEDS_STACK_FILES = frozenset({"check", "generate", "release", "test"})


def _refuse_unfinished_stack(command: str | None, rest: list[str]) -> None:
    """One line and exit 1 when ``command`` runs in an app whose stack files
    still carry a token (``sandloom add stack`` has not run)."""
    reads_files = command in _NEEDS_STACK_FILES or (
        command == "dev" and rest[:1] == ["up"]
    )
    if not reads_files or {"-h", "--help"} & set(rest):
        return
    problem = _static_import("sandloom.core.cli.stack_files").refusal(Path.cwd())
    if problem is not None:
        print(f"sandloom {command}: {problem}", file=sys.stderr)
        raise SystemExit(1)


def main() -> None:
    """The ``sandloom`` entry point: a connection failure is one line."""
    errors = _static_import("sandloom.core.cli.dev.errors")
    debug = errors.debug_requested(sys.argv, errors.environment())
    verbose = errors.verbose_requested(sys.argv)
    errors.quiet_logs(sys.argv, verbose=verbose or debug)
    if debug:
        # Child processes too (`dev run`'s server, the wizard's steps).
        errors.enable_debug(errors.environment())
    try:
        _main()
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as exc:
        if errors.report(exc, debug=debug):
            raise SystemExit(1) from None
        raise


#: ``sandloom --help``: every command once, grouped by what you are doing.
COMMAND_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "start",
        (
            ("new", "scaffold an application"),
            (
                "dev",
                "local PostgreSQL + Redis: up / status / run / reset / down / logs",
            ),
            ("docs", "the sandloom.sdk reference: sandloom docs <name>"),
            ("explain", "explain an error code: SANDLOOM-C001, C001 or a rule id"),
            ("version", "print the framework version"),
        ),
    ),
    (
        "build",
        (
            ("add", "generate code: module, event, contract, consumer, saga, ..."),
            ("generate", "write the generated files (snapshots, stubs, schemas)"),
            ("test", "module tests + fast suite + static gates"),
            ("check", "run the architecture (module isolation) checks"),
            ("migrate", "database migrations: new, plan, upgrade-all"),
            ("context", "compact module map with interfaces and file pointers"),
            ("graph", "offline architecture graph and module discovery"),
            ("explore", "(experimental) generate and open the architecture explorer"),
            ("contracts", "contract snapshots and compatibility checks"),
            ("routes", "route snapshots (each route's policy)"),
            ("release", "generate, and mark every contract and event released"),
        ),
    ),
    (
        "operate",
        (
            ("doctor", "read-only deployment preflight"),
            ("config", "the effective settings (sandloom config dump)"),
            ("keys", "generate signing keys"),
            (
                "admin",
                "platform administrators (create / invite / list) and the "
                "admin UI manifest (export)",
            ),
            ("worker", "run a task worker; `worker check` is the healthcheck"),
            ("beat", "run the task scheduler"),
            ("projection", "operate query projections"),
            ("outbox", "requeue dead-lettered outbox events"),
            ("streams", "replay the Streams dead-letter queue"),
            ("sagas", "list / show / retry / park / resolve sagas"),
            ("backup", "backup plan and restore verification"),
            ("security", "incident response (sessions, audit export)"),
        ),
    ),
)


def _grouped_command_help() -> str:
    """The command list of ``sandloom --help``, one block per group."""
    width = max(len(name) for _, entries in COMMAND_GROUPS for name, _ in entries)
    blocks = []
    for group, entries in COMMAND_GROUPS:
        lines = [f"{group}:"]
        lines += [f"  {name.ljust(width)}  {summary}" for name, summary in entries]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _main() -> None:  # noqa: C901
    parser = argparse.ArgumentParser(
        prog="sandloom",
        description="Sandloom framework CLI. `sandloom <command> --help` for one "
        "command; `-v` before the command shows the framework's INFO log lines.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_grouped_command_help(),
        usage="sandloom [-v] [--debug] <command> [args]",
    )
    sub = parser.add_subparsers(
        dest="command", metavar="<command>", help=argparse.SUPPRESS
    )
    for _group, entries in COMMAND_GROUPS:
        for name, _summary in entries:
            # Not listed by argparse itself: the epilog groups them.
            sub.add_parser(name, add_help=False)

    args, rest = parser.parse_known_args()
    _refuse_unfinished_stack(args.command, rest)
    if args.command in _CONNECTING_COMMANDS:
        errors = _static_import("sandloom.core.cli.dev.errors")
        errors.warn_env_drift(Path.cwd(), errors.environment())
    if args.command in _CONFIGURED_COMMANDS:
        _enforce_configuration()

    if args.command == "new":
        raise SystemExit(_cmd_new(rest))
    if args.command == "add":
        # `add deploy` builds the app to derive the profile: like `generate`,
        # it needs settings, not services (a new app has no URLs before
        # `sandloom dev up`).
        _load_test_env_for_authoring(create_database=False)
        raise SystemExit(_static_import("sandloom.core.cli.add").main(rest))
    if args.command == "check":
        raise SystemExit(_static_import("sandloom.core.cli.check").main(rest))
    if args.command == "generate":
        _load_test_env_for_authoring(create_database=False)
        raise SystemExit(_static_import("sandloom.core.cli.generated").main(rest))
    if args.command == "release":
        # `generate`, and in the same pass every contract and event of this
        # project is marked released (ADR-0017).
        _load_test_env_for_authoring(create_database=False)
        raise SystemExit(
            _static_import("sandloom.core.cli.generated").main(["--release", *rest])
        )
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
    if args.command == "worker":
        from sandloom.core.cli.worker import worker_main

        raise SystemExit(worker_main(rest))
    if args.command == "beat":
        from sandloom.core.cli.worker import beat_main

        raise SystemExit(beat_main(rest))
    if args.command == "dev":
        raise SystemExit(_static_import("sandloom.core.cli.dev").main(rest))
    if args.command == "version":
        print(_version())
        raise SystemExit(0)

    parser.print_help()
    raise SystemExit(0)


if __name__ == "__main__":  # pragma: no cover
    main()
