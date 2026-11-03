"""``sandloom new``: the wizard at a terminal, the flags everywhere else.

At a terminal (:func:`sandloom.core.cli.ui.interactive`) ``sandloom new``
welcomes the person and asks what its flags did not say: the name, the
standard modules, the coding agents, how to run PostgreSQL and Redis
(Docker, servers they already run, or later) and whether to set the app up
now. The setup runs ``uv sync``, ``sandloom generate``, ``dev up`` and the
migrations in the new app, one line per step. Last, with the console:
"Now create the console administrator", the email, and ``admin create``
attached to the terminal, which asks for the password itself: the wizard
never handles it. A summary says how to start the app, what to open, where
help is, and ends with the one command that does the same again without
questions. Like ``cargo new`` and ``rails new``, the app gets its own git
repository with a first commit (``--no-git``: none), unless it lives inside
an existing repository, which is never committed to.

A script, CI or a coding agent gets no question: the flags and the defaults
decide, and the output after "Next steps:" is the quickstart
(``tools/smoke/quickstart_check.py`` runs those lines). Design:
``design/cli-wizard/design.md`` (its revisions after the edge-case review).
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import urllib.parse
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import IO, Any, TextIO

import sandloom.core.cli.checkbox as _checkbox
import sandloom.core.cli.dev.commands as _dev_commands
import sandloom.core.cli.dev.envfile as _envfile
import sandloom.core.cli.dev.errors as _errors
import sandloom.core.cli.dev.identity as _identity
import sandloom.core.cli.dev.ports as _ports
import sandloom.core.cli.dev.process as _process
import sandloom.core.cli.dev.services as _services
import sandloom.core.cli.project as _project
import sandloom.core.cli.skills as _skills
import sandloom.core.cli.ui as _ui

#: The standard modules, in the generator's order, with one line each.
MODULES: tuple[tuple[str, str], ...] = (
    ("identity", "users, sign-in, email verification, 2FA"),
    ("tenancy", "organizations and members"),
    ("billing", "plans, payments, credits"),
    ("admin", "the web console for operators (admin brings identity)"),
)
DEFAULT_NAME = "my_app"
SITE_URL = "https://sandloom.dev"
DOCS_URL = "https://sandloom.dev/docs/"
#: The welcome under the caracal (one line at 80 columns).
WELCOME = "Welcome! This wizard guides you step by step to a new Sandloom application."
#: How the app's PostgreSQL and Redis run: the compose project, servers the
#: person already runs, or not decided yet (the files only).
DOCKER, EXTERNAL, LATER = "docker", "external", "none"
SERVICE_CHOICES = (DOCKER, EXTERNAL, LATER)
#: ``Machine.docker()``: Docker answers, is not installed, or is not running.
DOCKER_READY, DOCKER_MISSING, DOCKER_STOPPED = "ready", "missing", "stopped"
UV_INSTALL_URL = "https://docs.astral.sh/uv/getting-started/installation/"
#: Where the setup's command output goes (``.sandloom/run/`` is gitignored).
SETUP_LOG = Path(".sandloom/run/new-setup.log")
#: The variables a child ``uv`` must not inherit: they point it at the
#: caller's environment instead of the new app's.
_UV_LEAKS = frozenset({"VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PROJECT"})
DOCKER_TIMEOUT = 10.0
#: A log line (the framework's ``time [LEVEL] logger:``, or ``LEVEL:  ``):
#: kept in the log, never shown under the spinner.
_LOG_LINE = re.compile(
    r"^(?:\d{4}-\d\d-\d\d [\d:,.]+ \[\w+\] [\w.]+: "
    r"|(?:DEBUG|INFO|WARNING|ERROR|CRITICAL):\s)"
)
TAIL_LINES = 20
#: How long a stopped step may take to exit after Ctrl-C before it is killed.
STOP_GRACE = 15.0


class Cancelled(Exception):
    """The person left a question with Ctrl-C, Esc or the end of input."""


# --------------------------------------------------------------------------- #
# What was asked and decided
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Given:
    """What the flags already said (``None``: not said, so asked)."""

    app: str | None = None
    batteries: list[str] | None = None
    agents: list[str] | None = None
    setup: bool | None = None
    admin_email: str | None = None
    git: bool | None = None
    services: str | None = None
    database_url: str | None = None
    redis_url: str | None = None


@dataclass(frozen=True)
class Answers:
    app: str
    batteries: list[str]
    agents: list[str]
    setup: bool
    admin_email: str | None = None
    git: bool = True
    services: str = LATER
    database_url: str | None = None
    redis_url: str | None = None


@dataclass(frozen=True)
class Plan:
    """Everything that decides the generated app: what the repeat command
    carries (never the administrator)."""

    app: str
    parent: Path
    batteries: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=lambda: list(_skills.DEFAULT_AGENTS))
    module: str = "example"
    framework_req: str | None = None
    framework_source: str | None = None
    framework_path: Path | None = None
    setup: bool = False
    git: bool = True
    #: Only ``external`` shows in the repeat command (Docker is the default).
    services: str = DOCKER
    database_url: str | None = None
    redis_url: str | None = None


@dataclass
class Machine:
    """What the questions need to know about this machine (each probe is a
    callable, run only when its question comes)."""

    uv: str | None = None
    docker: Callable[[], str] = lambda: DOCKER_READY
    git_email: str | None = None
    #: The local PostgreSQL and Redis URLs that answer (``None``: nothing).
    local_services: Callable[[], tuple[str | None, str | None]] = lambda: (None, None)
    #: Why these servers cannot serve the app (one line), or ``None``.
    check_services: Callable[[str, str], str | None] = lambda db, redis: None


@dataclass
class Prompter:
    """Where questions go and answers come from. ``keys`` returns the raw
    keys for one checklist; ``None`` asks for typed answers instead
    (``TERM=dumb``, ``--plain``, a console without VT mode)."""

    out: TextIO
    style: _ui.Style
    read: Callable[[str], str]
    keys: Callable[[], Iterable[str]] | None = None

    def say(self, text: str = "") -> None:
        self.out.write(text + "\n")
        self.out.flush()

    def hint(self, text: str) -> None:
        self.say(self.style.muted(text))

    def ask(self, question: str) -> str:
        try:
            return self.read(self.style.prompt(question)).strip()
        except (KeyboardInterrupt, EOFError):
            self.say()
            raise Cancelled from None


# --------------------------------------------------------------------------- #
# The parser and the repeat command
# --------------------------------------------------------------------------- #


def parser() -> argparse.ArgumentParser:
    agents = ", ".join(a.key for a in _skills.CODING_AGENTS)
    p = argparse.ArgumentParser(
        prog="sandloom new",
        description="Scaffold a standalone Sandloom application. At a terminal "
        "it asks what the flags leave open; scripts and agents pass --yes.",
    )
    p.add_argument(
        "app",
        nargs="?",
        help="application name (snake_case, e.g. helpdesk); asked at a terminal",
    )
    p.add_argument(
        "--dir", type=Path, default=None, help="parent directory (default: cwd)"
    )
    p.add_argument(
        "--with",
        dest="batteries",
        default=None,
        help="comma-separated standard modules to install and load: "
        "identity,tenancy,billing,admin (the sandloom-<module> packages; "
        "admin brings identity)",
    )
    p.add_argument(
        "--no-batteries", action="store_true", help="install no standard module"
    )
    p.add_argument(
        "--module",
        default="example",
        help="name of the initial sample module (default: example)",
    )
    # For framework contributors (docs/contributing): where the app's
    # sandloom comes from. They work; --help keeps them out of a newcomer's way.
    p.add_argument("--framework-req", default=None, help=argparse.SUPPRESS)
    p.add_argument(
        "--framework-source",
        choices=("pypi", "git", "path"),
        default=None,
        help=argparse.SUPPRESS,
    )
    p.add_argument("--framework-path", type=Path, default=None, help=argparse.SUPPRESS)
    p.add_argument(
        "--agents",
        default=None,
        help=f"comma-separated coding agents to write skills for: {agents} "
        "(default: asked at a terminal, else the installed ones)",
    )
    p.add_argument(
        "-y",
        "--yes",
        "--no-input",
        dest="yes",
        action="store_true",
        help="ask nothing: the flags and the defaults decide (scripts and "
        "coding agents pass it)",
    )
    p.add_argument(
        "--setup",
        action="store_true",
        help="also run uv sync, sandloom generate, dev up and the migrations "
        "(asked at a terminal; not in CI)",
    )
    p.add_argument(
        "--admin-email",
        default=None,
        help="with --with admin --setup: create this administrator (without "
        "a terminal: `admin create --invite`, which prints a set-password link)",
    )
    p.add_argument(
        "--git",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="--no-git: no git repository (default: a repository with a first "
        "commit, unless the app lands inside an existing one)",
    )
    p.add_argument(
        "--services",
        choices=SERVICE_CHOICES,
        default=None,
        help="how --setup runs PostgreSQL and Redis: docker (the default), "
        "external (servers you already run: --database-url, --redis-url) or "
        "none (create the files only)",
    )
    p.add_argument(
        "--database-url",
        default=None,
        help="with --services external: your PostgreSQL, as a role that may "
        "create databases (default: postgresql://$USER@127.0.0.1:5432/postgres)",
    )
    p.add_argument(
        "--redis-url",
        default=None,
        help="with --services external: your Redis (default: redis://127.0.0.1:6379)",
    )
    p.add_argument(
        "--plain",
        action="store_true",
        help="no colour, no redrawn lines, typed answers (screen readers, logs)",
    )
    return p


def _split(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def plan_from_args(args: argparse.Namespace, *, cwd: Path) -> Plan:
    """The :class:`Plan` the flags of ``args`` describe (no question)."""
    batteries = [] if args.no_batteries else _split(args.batteries)
    agents = (
        _skills.parse_agents(args.agents)
        if args.agents
        else list(_skills.DEFAULT_AGENTS)
    )
    parent = cwd if args.dir is None else args.dir
    return Plan(
        app=args.app,
        parent=parent,
        batteries=_project_modules(batteries),
        agents=agents,
        module=args.module,
        framework_req=args.framework_req,
        framework_source=args.framework_source,
        framework_path=args.framework_path,
        setup=args.setup,
        git=args.git is not False,
        services=_services_flag(args) or DOCKER,
        database_url=args.database_url,
        redis_url=args.redis_url,
    )


def _services_flag(args: argparse.Namespace) -> str | None:
    """``--services``; the URL flags alone mean ``external``."""
    if args.services is None and (args.database_url or args.redis_url):
        return EXTERNAL
    return str(args.services) if args.services is not None else None


def repeat_command(version: str, plan: Plan, *, cwd: Path) -> list[str]:
    """The argv that makes the same app with no question, pinned to this
    version like the docs: ``uvx --from "sandloom==<v>" sandloom new …
    --yes``. It never carries the administrator's email or a password."""
    extra = "[standard]" if plan.batteries else ""
    argv = ["uvx", "--from", f"sandloom{extra}=={version}", "sandloom", "new", plan.app]
    if plan.parent.resolve() != cwd.resolve():
        argv += ["--dir", str(plan.parent)]
    if plan.batteries:
        argv += ["--with", ",".join(plan.batteries)]
    argv += ["--agents", ",".join(plan.agents)]
    if plan.module != "example":
        argv += ["--module", plan.module]
    if plan.framework_req:
        argv += ["--framework-req", plan.framework_req]
    if plan.framework_source:
        argv += ["--framework-source", plan.framework_source]
    if plan.framework_path is not None:
        argv += ["--framework-path", str(plan.framework_path)]
    argv += _repeat_choices(plan)
    argv.append("--yes")
    if plan.setup:
        argv.append("--setup")
    return argv


def _repeat_choices(plan: Plan) -> list[str]:
    """``--no-git`` and the person's own servers (the defaults stay out)."""
    argv = [] if plan.git else ["--no-git"]
    if plan.services == EXTERNAL:
        argv += ["--services", EXTERNAL]
        if plan.database_url:
            argv += ["--database-url", plan.database_url]
        if plan.redis_url:
            argv += ["--redis-url", plan.redis_url]
    return argv


def repeat_line(version: str, plan: Plan, *, cwd: Path) -> str:
    """:func:`repeat_command` as one shell line (the pin in double quotes,
    as the docs write it)."""
    argv = repeat_command(version, plan, cwd=cwd)
    # A database password stays off the screen: the line says where it goes.
    shown = [
        _services.redact(a) if i and argv[i - 1] in _URL_FLAGS else a
        for i, a in enumerate(argv)
    ]
    return " ".join(f'"{a}"' if i == 2 else shlex.quote(a) for i, a in enumerate(shown))


_URL_FLAGS = frozenset({"--database-url", "--redis-url"})


# --------------------------------------------------------------------------- #
# The questions
# --------------------------------------------------------------------------- #


def name_problem(name: str, parent: Path) -> str | None:
    """Why ``name`` cannot be the app (the generator's own rule, and the
    target directory must not exist), or ``None``."""
    problem = _project.app_name_problem(name)
    if problem is not None:
        return problem
    if (parent / name).exists():
        return f"{parent / name} already exists; choose another name"
    return None


def _project_modules(chosen: Iterable[str]) -> list[str]:
    """The generator's order; admin brings identity (its only dependency)."""
    wanted = set(chosen)
    if "admin" in wanted:
        wanted.add("identity")
    known = [name for name, _ in MODULES]
    unknown = sorted(wanted - set(known))
    if unknown:
        raise ValueError(
            f"unknown standard module(s) {', '.join(unknown)}; choose from "
            + ", ".join(known)
        )
    return [name for name in known if name in wanted]


def ask_name(p: Prompter, parent: Path) -> str:
    while True:
        name = (
            p.ask(f"What are you building? Name your app [{DEFAULT_NAME}]: ")
            or DEFAULT_NAME
        )
        problem = name_problem(name, parent)
        if problem is None:
            return name
        p.say(_ui.error_lines(p.style, problem))


def _typed_choice(answer: str) -> list[str]:
    names = [name for name, _ in MODULES]
    chosen = []
    for part in _split(answer.lower()):
        if part.isdigit() and 1 <= int(part) <= len(names):
            chosen.append(names[int(part) - 1])
        else:
            chosen.append(part)
    return chosen


#: Checking admin checks identity in the list (the generator adds it anyway).
_MODULE_REQUIRES = {
    [name for name, _ in MODULES].index("admin"): (
        [name for name, _ in MODULES].index("identity"),
    )
}


def ask_modules(p: Prompter) -> list[str]:
    p.say(p.style.bold("Standard modules") + p.style.muted(" (none is fine)"))
    width = max(len(name) for name, _ in MODULES)
    labels = [f"{name:<{width}}  {p.style.muted(line)}" for name, line in MODULES]
    if p.keys is not None:
        sym = p.style.symbols
        hint = f"  {sym.up}/{sym.down} move {sym.dot} space to add, Enter to continue"
        try:
            chosen = _checkbox.select(
                labels,
                (),
                keys=p.keys(),
                out=p.out,
                minimum=0,
                hint=p.style.muted(hint),
                requires=_MODULE_REQUIRES,
            )
        except (_checkbox.Cancelled, KeyboardInterrupt):
            raise Cancelled from None
        return _project_modules(MODULES[i][0] for i in chosen)
    for n, label in enumerate(labels, 1):
        p.say(f"  {n}. {label}")
    while True:
        answer = p.ask("Numbers or names, comma-separated (Enter: none): ")
        try:
            return _project_modules(_typed_choice(answer))
        except ValueError as exc:
            p.say(_ui.error_lines(p.style, str(exc)))


def ask_agents(
    p: Prompter, detected: dict[str, str], preselected: Sequence[str]
) -> list[str]:
    if p.keys is not None:
        try:
            return _skills.pick_agents(detected, preselected, keys=p.keys(), out=p.out)
        except (ValueError, KeyboardInterrupt):
            raise Cancelled from None
    return _skills.ask_agents(detected, preselected, read=p.ask, write=p.say)


def ask_yes(p: Prompter, question: str) -> bool:
    while True:
        answer = p.ask(f"{question} [Y/n] ").lower()
        if answer in {"", "y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False


def _address(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.hostname}:{parts.port}" if parts.port else str(parts.hostname)


def ask_services(p: Prompter, machine: Machine) -> tuple[str, str | None, str | None]:
    """How to run PostgreSQL and Redis: ``(choice, database URL, Redis URL)``.

    Docker is the default when it answers; otherwise the wizard says so
    plainly and defaults to servers that answer on the usual local ports, or
    to the files only."""
    sym = p.style.symbols
    status = machine.docker()
    found: tuple[str | None, str | None] | None = None
    p.say(
        p.style.bold("PostgreSQL and Redis")
        + p.style.muted(" (the app's database and cache)")
    )
    if status == DOCKER_READY:
        default = 1
    else:
        found = machine.local_services()
        default = 2 if any(found) else 3
    labels = [
        "Docker " + sym.dash + " Sandloom starts them for you",
        "I already run PostgreSQL and Redis (for example Homebrew or Postgres.app)",
        "Later " + sym.dash + " just create the files",
    ]
    for n, label in enumerate(labels, 1):
        p.say(f"  {n}. {label}")
    if status != DOCKER_READY:
        p.hint(_docker_said(status))
    while True:
        answer = p.ask(f"How do you want to run them? [{default}]: ") or str(default)
        if answer == "1" and _docker_now(p, machine, status):
            return DOCKER, None, None
        if answer == "2":
            urls = ask_external(p, machine, found)
            return (LATER, None, None) if urls is None else (EXTERNAL, *urls)
        if answer == "3":
            return LATER, None, None


def _docker_now(p: Prompter, machine: Machine, status: str) -> bool:
    """Docker chosen: it answers (perhaps started meanwhile), or says why not."""
    if status != DOCKER_READY:
        status = machine.docker()
    if status == DOCKER_READY:
        return True
    p.say(_ui.error_lines(p.style, _docker_said(status)))
    return False


def _docker_said(status: str) -> str:
    if status == DOCKER_MISSING:
        return (
            "Docker is not installed: choose 2 or 3 (or install Docker and run again)."
        )
    return "Docker is not running: start Docker Desktop to choose 1, or choose 2 or 3."


def ask_external(
    p: Prompter,
    machine: Machine,
    found: tuple[str | None, str | None] | None = None,
) -> tuple[str, str] | None:
    """The URLs of the servers the person runs, checked; ``None``: gave up
    (the files only)."""
    db, redis = found if found is not None else machine.local_services()
    for url, what in ((db, "PostgreSQL"), (redis, "Redis")):
        if url:
            p.say(f"  {p.style.ok(f'{what} answers at {_address(url)}')}")
    while True:
        db = _ask_url(
            p, "PostgreSQL URL", db, "postgresql://you@127.0.0.1:5432/postgres"
        )
        redis = _ask_url(p, "Redis URL", redis, "redis://127.0.0.1:6379")
        problem = machine.check_services(db, redis)
        if problem is None:
            p.say(f"  {p.style.ok('PostgreSQL and Redis answer')}")
            return db, redis
        p.say(_ui.error_lines(p.style, problem))
        if not ask_yes(p, "Try other URLs?"):
            return None


def _ask_url(p: Prompter, what: str, default: str | None, example: str) -> str:
    while True:
        question = (
            f"{what} [{default}]: " if default else f"{what} (for example {example}): "
        )
        answer = p.ask(question) or default
        if answer:
            return answer


def setup_question(services: str, *, admin: bool) -> str:
    """Says in plain words what the setup does, and about how long."""
    parts = ["Install the dependencies"]
    if services == DOCKER:
        parts.append("start PostgreSQL and Redis in Docker")
    if admin:
        text = ", ".join([*parts, "create the tables and your administrator"])
    else:
        text = ", ".join(parts) + " and create the tables"
    return f"{text} now? (about a minute)"


def ask_admin_email(p: Prompter, default: str | None) -> str | None:
    """The console administrator's email; ``None`` skips the step.

    ``default`` comes from ``git config user.email``, which may not be the
    address the person signs in with: taking it needs a confirmation, and a
    no asks again without it."""
    if default:
        answer = p.ask(
            f"Email of your console administrator [{default}] (from git config; "
            "- to skip): "
        )
        if answer == "-":
            return None
        if answer:
            return answer
        if ask_yes(
            p, f"Administrator: {default} — is this the address you sign in with?"
        ):
            return default
    return p.ask("Email of your console administrator (Enter to skip): ") or None


def ask(
    p: Prompter,
    given: Given,
    *,
    parent: Path,
    detected: dict[str, str],
    machine: Machine,
) -> Answers:
    """Ask, in order, what ``given`` leaves open (raises :class:`Cancelled`)."""
    p.hint("Scripts and agents: add --yes")
    app = given.app or ask_name(p, parent)
    batteries = (
        _project_modules(given.batteries)
        if given.batteries is not None
        else ask_modules(p)
    )
    if given.agents is not None:
        agents = list(given.agents)
    else:
        preselected = [a.key for a in _skills.CODING_AGENTS if a.key in detected]
        agents = ask_agents(p, detected, preselected or list(_skills.DEFAULT_AGENTS))
    services, db, redis = given.services, given.database_url, given.redis_url
    if machine.uv is None:
        p.hint(f"uv is not installed, so the setup is yours to run: {UV_INSTALL_URL}")
        services = services or LATER
    elif services is None:
        services, db, redis = ask_services(p, machine)
    admin = "admin" in batteries
    setup = False
    if machine.uv is not None and services != LATER:
        setup = bool(given.setup) or ask_yes(p, setup_question(services, admin=admin))
    # The administrator comes last, after the setup (create_administrator).
    return Answers(
        app,
        batteries,
        agents,
        setup,
        given.admin_email if setup else None,
        git=given.git is not False,
        services=services,
        database_url=db,
        redis_url=redis,
    )


# --------------------------------------------------------------------------- #
# The machine: uv, Docker, git
# --------------------------------------------------------------------------- #


def uv_binary(
    environ: Mapping[str, str], which: Callable[[str], str | None] = shutil.which
) -> str | None:
    """uv: the ``UV`` variable (uv sets it for its children), else ``PATH``."""
    return (environ.get("UV") or "").strip() or which("uv")


def child_env(environ: Mapping[str, str]) -> dict[str, str]:
    """``environ`` without the variables that tie uv to the caller's project."""
    return {k: v for k, v in environ.items() if k not in _UV_LEAKS}


def _run_quiet(
    argv: Sequence[str], environ: Mapping[str, str], *, cwd: Path | None = None
) -> subprocess.CompletedProcess[str] | None:
    """Run a probe (git, docker); ``None`` when it is missing or hangs."""
    if shutil.which(argv[0], path=environ.get("PATH")) is None:
        return None
    try:
        return _process.run(
            argv, env=environ, cwd=cwd, timeout=DOCKER_TIMEOUT, search_path=True
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def docker_status(environ: Mapping[str, str]) -> str:
    """:data:`DOCKER_READY` when ``docker info`` answers within
    :data:`DOCKER_TIMEOUT` seconds, :data:`DOCKER_MISSING` without the
    ``docker`` command, else :data:`DOCKER_STOPPED`."""
    if shutil.which("docker", path=environ.get("PATH")) is None:
        return DOCKER_MISSING
    proc = _run_quiet(["docker", "info", "--format", "{{.ServerVersion}}"], environ)
    return DOCKER_READY if proc is not None and proc.returncode == 0 else DOCKER_STOPPED


def docker_ready(environ: Mapping[str, str]) -> bool:
    return docker_status(environ) == DOCKER_READY


def git_toplevel(directory: Path, environ: Mapping[str, str]) -> Path | None:
    """The work tree holding ``directory`` (``git rev-parse --show-toplevel``),
    or ``None``. ``GIT_DIR`` and the like are ignored, as in ``sandloom dev``."""
    proc = _git(["rev-parse", "--show-toplevel"], environ, cwd=directory)
    if proc is None or proc.returncode != 0 or not proc.stdout.strip():
        return None
    return Path(proc.stdout.strip())


#: Variables a git hook sets that would point git at another repository.
_GIT_ENV = frozenset({"GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"})


def _git(
    args: Sequence[str], environ: Mapping[str, str], *, cwd: Path | None = None
) -> subprocess.CompletedProcess[str] | None:
    """``git <args>`` in ``cwd`` without :data:`_GIT_ENV`, so a hook's or an
    agent shell's ``GIT_DIR`` never points it at another repository."""
    env = {k: v for k, v in environ.items() if k not in _GIT_ENV}
    return _run_quiet(["git", *args], env, cwd=cwd)


def machine_for(environ: Mapping[str, str], parent: Path) -> Machine:
    """The real :class:`Machine` (the probes run when their question comes)."""
    return Machine(
        uv=uv_binary(environ),
        docker=lambda: docker_status(environ),
        git_email=git_email(environ),
        local_services=lambda: _dev_commands.local_servers(environ),
        check_services=_dev_commands.check_external,
    )


def git_email(environ: Mapping[str, str]) -> str | None:
    proc = _git(["config", "user.email"], environ)
    value = proc.stdout.strip() if proc is not None and proc.returncode == 0 else ""
    return value or None


def git_init(root: Path, environ: Mapping[str, str]) -> bool:
    """``git init`` in ``root`` (no commit), unless ``root`` is already inside
    a work tree or git is missing; whether it ran."""
    probe = root if root.is_dir() else root.parent
    inside = _git(["rev-parse", "--is-inside-work-tree"], environ, cwd=probe)
    if inside is None or inside.stdout.strip() == "true":
        return False
    done = _git(["init", "--quiet"], environ, cwd=root)
    return done is not None and done.returncode == 0


#: The message of the commit the wizard makes after its own ``git init``.
INITIAL_COMMIT = "Initial commit from sandloom new"


def _git_config(key: str, root: Path, environ: Mapping[str, str]) -> str:
    proc = _git(["config", key], environ, cwd=root)
    return proc.stdout.strip() if proc is not None and proc.returncode == 0 else ""


def initial_commit(root: Path, environ: Mapping[str, str]) -> str | None:
    """Commit the generated app in the repository ``git init`` just made
    (never another): ``None`` when it did, else the one-line hint to show
    instead. Needs git's ``user.name`` and ``user.email`` (never invented
    here). ``.env`` stays out: the generated ``.gitignore`` lists it. No
    hook runs and nothing is signed, for this commit only."""
    if not (
        _git_config("user.name", root, environ)
        and _git_config("user.email", root, environ)
    ):
        return (
            "No first commit: git has no user.name / user.email. Set them (git "
            'config --global user.name "Your Name"; git config --global '
            "user.email you@example.com) and run: git add -A && git commit -m "
            f'"{INITIAL_COMMIT}"; or once: git add -A && git -c user.name="Your '
            f'Name" -c user.email=you@example.com commit -m "{INITIAL_COMMIT}"'
        )
    added = _git(["add", "-A"], environ, cwd=root)
    if added is None or added.returncode != 0:
        return _commit_failed(added)
    done = _git(
        # This one automated commit only: no hook (none of this app's is
        # installed yet) and no signing (a key may need a passphrase or be
        # missing); the person's own commits keep their settings.
        [
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "--no-verify",
            "-m",
            INITIAL_COMMIT,
        ],
        environ,
        cwd=root,
    )
    if done is None or done.returncode != 0:
        return _commit_failed(done)
    return None


def _commit_failed(proc: subprocess.CompletedProcess[str] | None) -> str:
    """One line: why the first commit failed, and the command to make it."""
    lines = [] if proc is None else (proc.stderr or proc.stdout or "").splitlines()
    reason = next((line.strip() for line in reversed(lines) if line.strip()), "")
    return (
        f"No first commit ({reason or 'git commit failed'}); the repository "
        'stays. Commit by hand: git add -A && git commit -m "' + INITIAL_COMMIT + '"'
    )


def app_url_of(root: Path, environ: Mapping[str, str]) -> str | None:
    """Where the new app answers: the port its first ``dev up`` chose
    (``dev run``'s default), read back from the dev state."""
    try:
        return _dev_commands.app_url(root, environ)
    except OSError:
        return None


def free_app_port(preferred: int = 8000) -> int:
    """The port ``dev run`` takes: ``preferred``, else the next free one."""
    for port in range(preferred, preferred + 200):
        if _ports.port_free(port):
            return port
    return preferred


# --------------------------------------------------------------------------- #
# The setup runner
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Step:
    """One setup command: ``label`` is what the person would type (``uv run
    …``), ``argv`` what runs. An ``attached`` step owns the terminal (it asks
    its own questions); the others are captured to the log."""

    label: str
    argv: list[str]
    attached: bool = False


def setup_steps(
    uv: str,
    *,
    admin_email: str | None,
    attach: bool,
) -> list[Step]:
    """The setup commands, and the administrator when ``admin_email`` is
    given: at a terminal (``attach``) ``admin create`` itself asks for the
    password (the wizard never reads it); without one it prints a single-use
    set-password link (``--invite``)."""
    steps = [
        Step("uv sync", [uv, "sync"]),
        Step("uv run sandloom generate", [uv, "run", "sandloom", "generate"]),
        Step("uv run sandloom dev up", [uv, "run", "sandloom", "dev", "up"]),
        Step(
            "uv run sandloom migrate upgrade-all --apply",
            [uv, "run", "sandloom", "migrate", "upgrade-all", "--apply"],
        ),
    ]
    if admin_email:
        create = ["sandloom", "admin", "create", "--email", admin_email]
        if attach:
            steps.append(admin_step(uv, admin_email))
        else:
            create.append("--invite")
            steps.append(Step(f"uv run {shlex.join(create)}", [uv, "run", *create]))
    return steps


def admin_step(uv: str, email: str) -> Step:
    """``admin create`` on the terminal: it asks for the password twice and
    checks it; ``--quiet``: the wizard's summary says who signs in."""
    create = ["sandloom", "admin", "create", "--email", email]
    return Step(f"uv run {shlex.join(create)}", [uv, "run", *create, "--quiet"], True)


def _traceback_of(tail: Sequence[str], reason: str) -> bool:
    """Whether ``tail`` is a Python traceback whose last line is ``reason``
    (it repeats the reason with frames only a debugger needs)."""
    lines = [line for line in tail if line.strip()]
    if not lines or lines[-1].strip() != reason.strip():
        return False
    return any(
        line.startswith("Traceback (most recent call last)")
        or line.lstrip().startswith('File "')
        for line in lines
    )


@dataclass
class Result:
    ok: bool
    failed: int | None = None
    cancelled: bool = False
    #: Attached steps that did not finish (``admin create`` left with Ctrl-C).
    skipped: list[Step] = field(default_factory=list)


@dataclass
class SetupRunner:
    """Runs :class:`Step` s in the new app, one line each, output to the log.

    A failure prints the step's last :data:`TAIL_LINES` lines, its reason
    and ``Fix it and continue with: cd <app> && <the rest>``; the generated
    files stay. Ctrl-C stops the running step (a second one kills it) and
    prints the same line, never a traceback."""

    out: TextIO
    style: _ui.Style
    environ: Mapping[str, str]
    log: Path
    popen: Callable[..., Any] = subprocess.Popen
    width: int = 80

    def run(self, steps: Sequence[Step], *, cwd: Path, app_dir: str) -> Result:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        env = child_env(self.environ)
        skipped: list[Step] = []
        with self.log.open("a", encoding="utf-8") as log:
            for index, step in enumerate(steps):
                log.write(f"$ {step.label}\n")
                log.flush()
                outcome = self._step(step, cwd=cwd, env=env, log=log)
                if outcome == "ok":
                    continue
                if outcome == "skipped":  # the attached last step: reported after
                    skipped.append(step)
                    continue
                self._continue_with(
                    steps[index:], app_dir, cancelled=outcome == "cancelled"
                )
                return Result(False, index, outcome == "cancelled")
        return Result(True, skipped=skipped)

    def _continue_with(
        self, rest: Sequence[Step], app_dir: str, *, cancelled: bool
    ) -> None:
        # One command per line: a narrow terminal or a log viewer cuts no
        # command off, and each line can be pasted on its own.
        commands = ["cd " + shlex.quote(app_dir), *(s.label for s in rest)]
        head = "Cancelled. Continue with" if cancelled else "Fix it and continue with"
        self.out.write(f"\n{head}:\n" + "".join(f"  {c}\n" for c in commands))
        self.out.flush()

    def _step(self, step: Step, *, cwd: Path, env: dict[str, str], log: IO[str]) -> str:
        if step.attached:
            return self._attached(step, cwd=cwd, env=env)
        line = _ui.StepLine(self.style, self.out, step.label, width=self.width).start()
        tail: deque[str] = deque(maxlen=TAIL_LINES)
        errors: list[str] = []
        try:
            proc = self._spawn(step, cwd=cwd, env=env)
        except OSError as exc:
            line.finish(ok=False)
            self.out.write(_ui.error_lines(self.style, str(exc)) + "\n")
            return "failed"
        readers = [
            threading.Thread(
                target=self._pump, args=(stream, log, tail, line, sink), daemon=True
            )
            for stream, sink in ((proc.stdout, None), (proc.stderr, errors))
            if stream is not None
        ]
        try:
            for reader in readers:
                reader.start()
            code, cancelled = self._wait(proc)
        except KeyboardInterrupt:  # Ctrl-C before the wait began
            code, cancelled = self._stop(proc), True
        for reader in readers:
            if reader.ident is not None:  # started, even if start() was interrupted
                reader.join()
        line.finish(ok=code == 0 and not cancelled)
        if cancelled:
            return "cancelled"
        if code == 0:
            return "ok"
        self._report(tail, errors, code)
        return "failed"

    def _spawn(self, step: Step, *, cwd: Path, env: dict[str, str]) -> Any:
        """Start a captured step: no stdin, its output to pipes."""
        return self.popen(
            step.argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )

    @staticmethod
    def _pump(
        stream: IO[str],
        log: IO[str],
        tail: deque[str],
        line: _ui.StepLine,
        errors: list[str] | None,
    ) -> None:
        for raw in stream:
            text = raw.rstrip("\n")
            log.write(raw if raw.endswith("\n") else raw + "\n")
            log.flush()
            tail.append(text)
            if errors is not None and text.strip():
                # stderr: what the step says it is doing, under the spinner;
                # a log line (the framework's, uvicorn's) is never shown there.
                if not _LOG_LINE.match(text):
                    line.note(text)
                errors.append(text.strip())

    @staticmethod
    def _wait(proc: Any) -> tuple[int, bool]:
        """The exit status; on Ctrl-C the step is asked to stop, then waited
        for (a second Ctrl-C, or :data:`STOP_GRACE`, kills it)."""
        try:
            return proc.wait(), False
        except KeyboardInterrupt:
            return SetupRunner._stop(proc), True

    @staticmethod
    def _stop(proc: Any) -> int:
        """Ask the step to stop, wait :data:`STOP_GRACE`, then kill it."""
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=STOP_GRACE)
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            proc.wait()
        return proc.returncode or 1

    def _report(self, tail: deque[str], errors: list[str], code: int) -> None:
        """The reason first; then the last lines, unless they are only a
        traceback ending in that reason; then where the whole output is."""
        reason = errors[-1] if errors else f"exit status {code}"
        self.out.write(_ui.error_lines(self.style, reason) + "\n")
        if not _traceback_of(tail, reason):
            for text in tail:
                self.out.write(f"    {self.style.muted(text)}\n")
        self.out.write(self.style.muted(f"  The whole output: {self.log}") + "\n")

    def _attached(self, step: Step, *, cwd: Path, env: dict[str, str]) -> str:
        """A step on the terminal (``admin create`` asks for the password):
        the terminal's Ctrl-C reaches it, this process waits."""
        sym = self.style.symbols
        self.out.write(f"  {self.style.accent(sym.pointer)} {step.label}\n")
        self.out.flush()
        try:
            # Spawned first: a child would inherit an ignored SIGINT.
            proc = self.popen(step.argv, cwd=cwd, env=env)
        except OSError as exc:
            self.out.write(_ui.error_lines(self.style, str(exc)) + "\n")
            return "skipped"
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            code = proc.wait()
        finally:
            signal.signal(signal.SIGINT, previous)
        if code == 0:
            self.out.write(f"  {self.style.ok(step.label)}\n")
            return "ok"
        self.out.write(f"  {self.style.fail(step.label)}\n")
        return "skipped"


# --------------------------------------------------------------------------- #
# sandloom new
# --------------------------------------------------------------------------- #


@dataclass
class _Run:
    """One ``sandloom new``: its streams, environment and decisions."""

    args: argparse.Namespace
    environ: Mapping[str, str]
    stdin: Any
    out: TextIO
    err: TextIO
    version: str
    asking: bool
    style: _ui.Style
    #: The questions' prompter (at a terminal), for the administrator's email.
    prompter: Prompter | None = None
    git_email: str | None = None

    def fail(self, message: str, code: int = 1) -> int:
        self.err.write(f"error: {message}\n")
        self.err.flush()
        return code


def _version() -> str:
    try:
        return metadata.version("sandloom")
    except metadata.PackageNotFoundError:  # an unbuilt tree has no dist metadata
        return "0.0.0+local"


def _reader(run: _Run) -> Callable[[str], str]:
    if run.stdin is sys.stdin and run.out is sys.stdout:
        return input  # line editing at the terminal

    def read(prompt: str) -> str:
        run.out.write(prompt)
        run.out.flush()
        line = run.stdin.readline()
        if not line:
            raise EOFError
        return str(line).rstrip("\r\n")

    return read


def _given(run: _Run) -> Given | int:
    """The flags as :class:`Given`, or an exit status for a bad flag."""
    args = run.args
    batteries: list[str] | None = None
    if args.no_batteries:
        batteries = []
    elif args.batteries is not None:
        batteries = _split(args.batteries)
    try:
        if batteries is not None:
            batteries = _project_modules(batteries)
        agents = _skills.parse_agents(args.agents) if args.agents else None
    except ValueError as exc:
        return run.fail(str(exc))
    known = batteries if batteries is not None else ([] if not run.asking else None)
    if args.admin_email and known is not None and "admin" not in known:
        return run.fail("--admin-email needs --with admin", 2)
    if args.app is not None:
        problem = _project.app_name_problem(args.app)
        if problem is not None:
            return run.fail(problem)
    services = _services_flag(args)
    if services != EXTERNAL and (args.database_url or args.redis_url):
        return run.fail("--database-url and --redis-url need --services external", 2)
    if services == LATER and args.setup:
        return run.fail("--services none creates the files only: leave --setup out", 2)
    return Given(
        app=args.app,
        batteries=batteries,
        agents=agents,
        setup=True if args.setup else None,
        admin_email=args.admin_email,
        git=args.git,
        services=services,
        database_url=args.database_url,
        redis_url=args.redis_url,
    )


def _external_without_questions(run: _Run, given: Given) -> Given | int:
    """``--services external`` with ``--yes``: the URLs given, else the local
    servers that answer; both checked before anything is written."""
    db, redis = given.database_url, given.redis_url
    if not db or not redis:
        found_db, found_redis = _dev_commands.local_servers(run.environ)
        db, redis = db or found_db, redis or found_redis
    if not db or not redis:
        missing = (
            "PostgreSQL on 127.0.0.1:5432" if not db else "Redis on 127.0.0.1:6379"
        )
        return run.fail(
            f"--services external: no {missing} answers; pass --database-url and "
            "--redis-url"
        )
    problem = _dev_commands.check_external(db, redis)
    if problem is not None:
        return run.fail(problem)
    return dataclasses.replace(given, database_url=db, redis_url=redis)


def welcome(
    out: TextIO, style: _ui.Style, version: str, *, width: int, height: int
) -> None:
    """The caracal and the name, then the site and what the wizard does."""
    for line in _ui.heading(
        style, version, "new application", width=width, height=height, mark=True
    ):
        out.write(line + "\n")
    out.write(f"\n  {style.accent(SITE_URL)}\n  {style.bold(WELCOME)}\n\n")
    out.flush()


def _answers(run: _Run, given: Given) -> Answers | int:
    """Ask at a terminal; elsewhere the flags and the defaults decide."""
    if not run.asking:
        if not given.app:
            return run.fail(
                "the app name is required without a terminal: sandloom new <app> "
                "(e.g. sandloom new shop --yes)",
                2,
            )
        if given.services == EXTERNAL:
            checked = _external_without_questions(run, given)
            if isinstance(checked, int):
                return checked
            given = checked
        agents = given.agents or _skills.choose_agents(None, None, interactive=False)
        setup = bool(given.setup)
        return Answers(
            str(given.app),
            given.batteries or [],
            agents,
            setup,
            given.admin_email,
            git=given.git is not False,
            services=given.services or (DOCKER if setup else LATER),
            database_url=given.database_url,
            redis_url=given.redis_url,
        )
    welcome(
        run.out,
        run.style,
        run.version,
        width=_ui.columns(run.out),
        height=_ui.rows(run.out),
    )
    keys = None
    if run.style.motion and _checkbox.available():
        keys = _checkbox.terminal_keys
    prompter = Prompter(run.out, run.style, _reader(run), keys)
    machine = machine_for(run.environ, run.args.dir)
    run.prompter, run.git_email = prompter, machine.git_email
    try:
        answers = ask(
            prompter,
            given,
            parent=run.args.dir,
            detected=_skills.detect_agents(),
            machine=machine,
        )
    except Cancelled:
        run.out.write("Cancelled; nothing was created\n")
        return 130
    if answers.admin_email and "admin" not in answers.batteries:
        return run.fail("--admin-email needs --with admin", 2)
    return answers


def _generate(run: _Run, plan: Plan) -> list[Path] | int:
    try:
        return _project.generate_project(
            plan.app,
            plan.parent,
            with_batteries=plan.batteries,
            sample_module=plan.module,
            framework_req=plan.framework_req,
            framework_source=plan.framework_source,
            framework_path=plan.framework_path,
            agents=plan.agents,
        )
    except FileExistsError:
        return run.fail(
            f"{plan.parent / plan.app} already exists: choose another name, or "
            "another parent directory with --dir"
        )
    except ValueError as exc:
        return run.fail(str(exc))


def _created(run: _Run, plan: Plan, created: list[Path]) -> Path:
    root = plan.parent / plan.app
    if run.asking:
        run.out.write("\n")
    run.out.write(
        f"Created application '{plan.app}' ({len(created)} files) at {root}\n"
    )
    run.out.write(
        f"Agent skills for: {', '.join(plan.agents)} "
        "(change with `uv run sandloom add skills --choose`)\n"
    )
    # The generated files (snapshots, schemas, stubs): the app's own CI gate
    # (`sandloom test --ci`) only checks them, so a fresh app must carry them.
    # A setup writes them in the app's own environment (`sandloom generate`).
    if plan.setup:
        return root
    problem = _project.write_generated_files(root)
    if problem is not None:
        run.out.write(
            f"note: generated files not written ({problem}); "
            "run `uv run sandloom generate` once the app installs\n"
        )
    run.out.flush()
    return root


def _app_dir(root: Path) -> str:
    """How to ``cd`` into the app from here."""
    return root.name if root.parent.resolve() == Path.cwd().resolve() else str(root)


def _serve_line(run: _Run, root: Path) -> str:
    """``dev run``: on the port the first ``dev up`` chose, else 8000 or the
    next free one."""
    serve = "uv run sandloom dev run"
    if app_url_of(root, run.environ) is None:
        port = free_app_port()
        serve += f" --port {port}" if port != 8000 else ""
    return serve


def _next_steps(run: _Run, plan: Plan, root: Path, answers: Answers) -> list[str]:
    """The quickstart lines, or after a setup the ones left to run."""
    in_cwd = plan.parent.resolve() == Path.cwd().resolve()
    lines = _project.quickstart(
        plan.app, plan.module, path=None if in_cwd else str(root)
    )
    if answers.setup:
        lines = [lines[0], _serve_line(run, root)]
        if "admin" in plan.batteries and not answers.admin_email:
            lines.insert(1, _project.ADMIN_NEXT_STEP)
    elif "admin" in plan.batteries:
        lines.append(_project.ADMIN_NEXT_STEP)
    return lines


def _setup(run: _Run, root: Path, answers: Answers) -> Result:
    uv = uv_binary(run.environ)
    if uv is None:
        run.err.write(f"error: --setup needs uv: {UV_INSTALL_URL}\n")
        return Result(False)
    if "admin" in answers.batteries and not answers.admin_email and not run.asking:
        run.out.write(
            run.style.muted("  The administrator step is skipped (no --admin-email).")
            + "\n"
        )
    # At a terminal the administrator comes after (create_administrator).
    steps = setup_steps(
        uv, admin_email=None if run.asking else answers.admin_email, attach=False
    )
    runner = SetupRunner(
        out=run.out,
        style=run.style,
        environ=run.environ,
        log=root / SETUP_LOG,
        width=_ui.columns(run.out),
    )
    return runner.run(steps, cwd=root, app_dir=_app_dir(root))


def configure_external(run: _Run, root: Path, answers: Answers) -> str | None:
    """Servers the person runs: keep the choice in the dev state, create the
    app's database, write the ``.env`` block and ``.env.test.local`` (what
    ``dev up --services external`` does, without Docker). The problem in one
    line, or ``None``."""
    ctx = _dev_commands.Context(
        identity=_identity.resolve(root, run.environ),
        environ=run.environ,
        out=io.StringIO(),
        err=io.StringIO(),
    )
    options = _dev_commands.UpOptions(
        services=EXTERNAL,
        database_url=answers.database_url,
        redis_url=answers.redis_url,
    )
    try:
        _dev_commands.up_external(ctx, options)
    except (_dev_commands.DevError, _envfile.EnvFileChanged, OSError) as exc:
        return str(exc)
    return None


def _start_lines(run: _Run, plan: Plan, root: Path, answers: Answers) -> list[str]:
    """How to start the app: after a setup, one line; otherwise the setup's
    own commands, one per line, then ``dev run``."""
    serve = _serve_line(run, root)
    cd = "cd " + shlex.quote(_app_dir(root))
    if answers.setup:
        return [f"{cd} && {serve}"]
    steps = setup_steps(
        "uv",
        admin_email="you@example.com" if "admin" in plan.batteries else None,
        attach=True,
    )
    return [cd, *(step.label for step in steps), serve]


def _summary(
    run: _Run,
    plan: Plan,
    root: Path,
    answers: Answers,
    *,
    admin_created: bool = False,
) -> str:
    """One block: ready (or created), how to start it, what to open after
    that; then help, set apart; the repeat command last, for scripts."""
    style = run.style
    sym = style.symbols
    url = app_url_of(root, run.environ) or f"http://127.0.0.1:{free_app_port()}"
    console = "admin" in plan.batteries
    signed_in = console and admin_created and bool(answers.admin_email)
    title = "Your app is ready" if answers.setup else "Your app is created"
    lines = ["", f"  {style.ok(style.bold(title))}  {style.muted(plan.app)}", ""]
    head = "Start it:" if answers.setup else "Set it up and start it:"
    lines.append("  " + style.bold(head))
    lines += [
        f"      {style.prompt(line)}" for line in _start_lines(run, plan, root, answers)
    ]
    lines += ["", "  " + style.bold("Then open:")]
    opened = [
        ("App", style.accent(f"{url}/")),
        ("API docs", style.accent(f"{url}/docs")),
    ]
    if console:
        opened.append(("Admin console", style.accent(f"{url}/admin/")))
    width = max(len(label) for label, _ in opened)
    lines += [f"      {label.ljust(width)}  {value}" for label, value in opened]
    pad = " " * (6 + width + 2)
    if signed_in:
        lines.append(pad + style.bold(f"Sign in as {answers.admin_email}"))
    elif console and answers.setup:
        lines.append(
            pad + "No administrator yet: uv run sandloom admin create --email "
            "you@example.com"
        )
    help_rows: list[tuple[str, str]] = []
    if signed_in:
        email = shlex.quote(answers.admin_email or "")
        help_rows += [
            ("Forgot the password?", "uv run sandloom admin list  (who can sign in)"),
            ("", f"uv run sandloom admin invite --email {email}  (a new password)"),
        ]
    if answers.services != EXTERNAL and not answers.setup:
        help_rows.append(
            (
                "No Docker?",
                "uv run sandloom dev up --services external  (your own "
                "PostgreSQL and Redis)",
            )
        )
    help_rows.append(("Docs", DOCS_URL))
    label_width = max(len(label) for label, _ in help_rows)
    lines += ["", "  " + style.muted("Help")]
    lines += [
        f"      {style.muted(label.ljust(label_width))}  {style.muted(value)}"
        for label, value in help_rows
    ]
    lines += [
        "",
        "  "
        + style.muted(
            f"For scripts and agents {sym.dash} the same, without questions:"
        ),
        "  " + style.muted(repeat_line(run.version, plan, cwd=Path.cwd())),
    ]
    return "\n".join(lines) + "\n"


def _start_git(run: _Run, root: Path, answers: Answers) -> bool:
    """``git init`` (before the setup, so the dev state lands in the app's
    own repository); whether it ran. Never inside another repository: the
    files stay there, uncommitted."""
    if not answers.git:
        return False
    muted = run.style.muted
    if shutil.which("git", path=run.environ.get("PATH")) is None:
        run.out.write(f"  {muted('git is not installed: no repository.')}\n")
        return False
    top = git_toplevel(root.parent, run.environ)
    if top is not None:
        line = (
            f"inside the git repository at {top}: the files are added there, "
            "not committed"
        )
        home = run.environ.get("HOME")
        if home and Path(home).resolve() == top.resolve():
            line += " (your home directory is a git repository)"
        run.out.write(f"  {muted(line)}\n")
        return False
    if git_init(root, run.environ):
        return True
    run.out.write(f"  {muted('No git repository: git init failed.')}\n")
    return False


def _own_servers(run: _Run, plan: Plan, root: Path, answers: Answers) -> Answers:
    """Configure the person's own servers; on a failure the setup is off:
    the summary lists the steps left."""
    problem = configure_external(run, root, answers)
    if problem is None:
        pg = _services.Postgres.from_url(answers.database_url or "")
        done = f"PostgreSQL and Redis: the database {plan.app} on {pg.address}"
        run.out.write(f"  {run.style.ok(done)}\n")
        return answers
    run.out.write(_ui.error_lines(run.style, problem) + "\n")
    return dataclasses.replace(answers, setup=False, admin_email=None)


def create_administrator(
    run: _Run, root: Path, app: str, email: str | None, popen: Any = None
) -> str | None:
    """After the setup: "Now create the console administrator", the email,
    then ``admin create`` attached to the terminal, which asks for the
    password itself (the wizard never reads it). The address created, or
    ``None`` (skipped, Ctrl-C, or ``admin create`` gave up)."""
    p, uv = run.prompter, uv_binary(run.environ)
    if p is None or uv is None:
        return None
    p.say()
    p.say("  " + run.style.bold(f"Now create the console administrator of {app}"))
    try:
        email = email or ask_admin_email(p, run.git_email)
    except Cancelled:
        email = None
    if not email:
        p.hint("  No administrator now: the summary says how to create one.")
        return None
    runner = SetupRunner(
        out=run.out,
        style=run.style,
        environ=run.environ,
        log=root / SETUP_LOG,
        popen=popen or subprocess.Popen,
        width=_ui.columns(run.out),
    )
    result = runner.run([admin_step(uv, email)], cwd=root, app_dir=_app_dir(root))
    return email if result.ok and not result.skipped else None


def _finish(run: _Run, plan: Plan, root: Path, answers: Answers) -> int:
    style = run.style
    initialised = _start_git(run, root, answers)
    servers_failed = False
    if answers.services == EXTERNAL:
        requested = answers.setup
        answers = _own_servers(run, plan, root, answers)
        servers_failed = requested and not answers.setup
    admin_created = False
    if answers.setup:
        result = _setup(run, root, answers)
        if not result.ok:
            return 130 if result.cancelled else 1
        if run.asking and "admin" in answers.batteries:
            created = create_administrator(run, root, plan.app, answers.admin_email)
            answers = dataclasses.replace(answers, admin_email=created)
            admin_created = created is not None
        else:
            admin_created = bool(answers.admin_email) and not result.skipped
    if initialised:
        # After the setup: the files `sandloom generate` writes are in it too.
        hint = initial_commit(root, run.environ)
        if hint is None:
            run.out.write(f"  {style.ok('git repository with a first commit')}\n")
        else:
            run.out.write(f"  {style.ok('git repository')}\n  {style.muted(hint)}\n")
    if not run.asking:
        # CI parses this block (tools/smoke/quickstart_check.py): plain mode
        # prints nothing after it.
        lines = _next_steps(run, plan, root, answers)
        run.out.write("\nNext steps:\n" + "".join(f"  {line}\n" for line in lines))
        # A script that asked for the setup sees from the status it did not run.
        return 1 if servers_failed else 0
    run.out.write(_summary(run, plan, root, answers, admin_created=admin_created))
    run.out.flush()
    return 0


def main(
    argv: list[str],
    *,
    environ: Mapping[str, str] | None = None,
    stdin: Any = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    version: str | None = None,
) -> int:
    """``sandloom new``; returns the exit status."""
    args = parser().parse_args(argv)
    if args.dir is None:
        args.dir = Path.cwd()
    env = _errors.environment() if environ is None else environ
    stdin = sys.stdin if stdin is None else stdin
    out = sys.stdout if out is None else out
    asking = _ui.interactive(env, stdin=stdin, stdout=out, no_input=args.yes)
    run = _Run(
        args=args,
        environ=env,
        stdin=stdin,
        out=out,
        err=sys.stderr if err is None else err,
        version=version or _version(),
        asking=asking,
        style=_ui.style_for(out, env, plain=args.plain),
    )
    if args.setup and _ui.in_ci(env):
        return run.fail(
            "--setup starts the dev services, and CI provides its own services "
            "(`sandloom dev up` refuses there): leave --setup out"
        )
    given = _given(run)
    if isinstance(given, int):
        return given
    answers = _answers(run, given)
    if isinstance(answers, int):
        return answers
    plan = Plan(
        app=answers.app,
        parent=args.dir,
        batteries=answers.batteries,
        agents=answers.agents,
        module=args.module,
        framework_req=args.framework_req,
        framework_source=args.framework_source,
        framework_path=args.framework_path,
        setup=answers.setup,
        git=answers.git,
        services=EXTERNAL if answers.services == EXTERNAL else DOCKER,
        database_url=answers.database_url,
        redis_url=answers.redis_url,
    )
    created = _generate(run, plan)
    if isinstance(created, int):
        return created
    root = _created(run, plan, created)
    return _finish(run, plan, root, answers)


__all__ = [
    "MODULES",
    "Answers",
    "Cancelled",
    "Given",
    "Machine",
    "Plan",
    "Prompter",
    "Result",
    "SetupRunner",
    "Step",
    "ask",
    "ask_services",
    "admin_step",
    "child_env",
    "create_administrator",
    "configure_external",
    "docker_ready",
    "docker_status",
    "git_init",
    "git_toplevel",
    "initial_commit",
    "machine_for",
    "main",
    "name_problem",
    "parser",
    "plan_from_args",
    "repeat_command",
    "repeat_line",
    "setup_steps",
    "uv_binary",
    "welcome",
]
