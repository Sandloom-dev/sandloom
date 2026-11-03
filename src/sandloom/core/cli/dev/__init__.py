"""``sandloom dev``: local Postgres and Redis without collisions.

One set of containers per **repository** (the main checkout and its git
worktrees share them), one database and one pair of Redis indexes per
**checkout**, one test database per **test run**::

    sandloom dev up                   start / check the services, write this
                                      checkout's .env block and .env.test.local
    sandloom dev status [--json]      services, ports, reachability, URLs
    sandloom dev run [--port N|auto]  uvicorn for this checkout (default 8000)
    sandloom dev reset [--force]      drop and recreate this checkout's database
    sandloom dev down [--all] [--volumes] [--force]
    sandloom dev prune                databases of removed worktrees, stale test
                                      databases, ports of removed repositories
    sandloom dev logs [service] [-f]

``up`` is idempotent and safe to run from every agent first thing: a second
``up`` waits for the first (the repository lock) and finds everything up.
Commands that need a database never start the services themselves; they say
so in one line (SANDLOOM-R014). State: ``<git common dir>/sandloom/``
(:mod:`sandloom.core.cli.dev.identity`), locks
(:mod:`sandloom.core.cli.dev.locks`), ports
(:mod:`sandloom.core.cli.dev.ports`), the ``.env`` block
(:mod:`sandloom.core.cli.dev.envfile`). Design:
``design/dev-services/design.md``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from pathlib import Path

from sandloom.core.cli.dev import commands as _commands
from sandloom.core.cli.dev import docker as _docker
from sandloom.core.cli.dev import errors as _errors
from sandloom.core.cli.dev import identity as _identity


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sandloom dev",
        description="Local Postgres and Redis for this repository and checkout",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    up = sub.add_parser("up", help="start the services, write this checkout's .env")
    up.add_argument(
        "--recreate",
        action="store_true",
        help="apply a changed deploy/docker-compose.dev.yml to running services",
    )
    up.add_argument(
        "--force",
        action="store_true",
        help="recreate even while other checkouts use the services",
    )
    up.add_argument(
        "--adopt",
        action="store_true",
        help="keep the services of a moved repository or of an old `<app>-dev` "
        "compose project",
    )
    up.add_argument("--no-pull", action="store_true", help="never pull images")
    status = sub.add_parser("status", help="services, ports, URLs")
    status.add_argument("--json", action="store_true", help="machine-readable")
    run = sub.add_parser("run", help="run the app (uvicorn) for this checkout")
    run.add_argument("--port", default=None, help="a port number, or auto")
    run.add_argument("--no-reload", action="store_true", help="no auto-reload")
    reset = sub.add_parser("reset", help="drop and recreate this checkout's database")
    reset.add_argument("--force", action="store_true", help="even while in use")
    down = sub.add_parser("down", help="stop the repository's services")
    down.add_argument(
        "--all", action="store_true", help="even while other checkouts use them"
    )
    down.add_argument(
        "--volumes", action="store_true", help="also delete the data (with --all)"
    )
    down.add_argument(
        "--force", action="store_true", help="even while this checkout uses them"
    )
    sub.add_parser("prune", help="drop what removed worktrees and old runs left")
    logs = sub.add_parser("logs", help="the services' logs")
    logs.add_argument("service", nargs="?", help="db, redis, postgres_test, redis_test")
    logs.add_argument("-f", "--follow", action="store_true")
    return parser


def _dispatch(ctx: _commands.Context, args: argparse.Namespace) -> int:
    if args.action == "up":
        options = _commands.UpOptions(
            recreate=args.recreate,
            force=args.force,
            adopt=args.adopt,
            pull=not args.no_pull,
        )
        return _commands.up(ctx, options)
    if args.action == "status":
        return _commands.status(ctx, as_json=args.json)
    if args.action == "run":
        return _commands.run(ctx, port=args.port, reload=not args.no_reload)
    if args.action == "reset":
        return _commands.reset(ctx, force=args.force)
    if args.action == "down":
        return _commands.down(
            ctx, all_=args.all, volumes=args.volumes, force=args.force
        )
    if args.action == "prune":
        return _commands.prune(ctx)
    return _commands.logs(ctx, service=args.service, follow=args.follow)


def _ci(action: str, env: Mapping[str, str]) -> int:
    if action == "up" and env.get("SANDLOOM_TEST_DATABASE_URL"):
        print(
            "sandloom dev up: CI provides the services (CI is set and "
            "SANDLOOM_TEST_DATABASE_URL names the test database); nothing to start"
        )
        return 0
    print(
        f"error: `sandloom dev {action}` is for a developer machine; in CI, "
        "provide the services and set SANDLOOM_TEST_DATABASE_URL, "
        "SANDLOOM_TEST_CACHE_URL and SANDLOOM_TEST_BROKER_URL",
        file=sys.stderr,
    )
    return 1


def main(
    argv: list[str] | None = None, *, environ: Mapping[str, str] | None = None
) -> int:
    args = _parser().parse_args(argv)
    env = _errors.environment() if environ is None else environ
    if _commands.in_ci(env) and args.action != "run":
        return _ci(args.action, env)
    ctx = _commands.Context(
        identity=_identity.resolve(Path.cwd(), env),
        environ=env,
        interactive=sys.stdin.isatty() and sys.stdout.isatty(),
    )
    try:
        return _dispatch(ctx, args)
    except _docker.DockerUnavailable as exc:
        print(
            f"error: Docker is not reachable ({exc}). Start Docker, or point the "
            "tests at an existing server with SANDLOOM_TEST_DATABASE_URL",
            file=sys.stderr,
        )
        return 1
    except _commands.DevError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


__all__ = ["main"]
