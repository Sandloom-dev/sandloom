"""``sandloom new``: the wizard at a terminal, the flags everywhere else.

At a terminal (:func:`sandloom.core.cli.ui.interactive`) ``sandloom new`` asks
what its flags did not say: the name, the standard modules, the coding
agents, whether to set the app up now, and the first administrator. The
setup then runs ``uv sync``, ``sandloom generate``, ``dev up``, the
migrations and ``admin create`` in the new app, one line per step, and a
summary ends with the one command that does the same again without
questions. A script, CI or a coding agent gets no question: the flags and
the defaults decide, and the output after "Next steps:" is the quickstart
(``tools/smoke/quickstart_check.py`` runs those lines). Design:
``design/cli-wizard/design.md`` (its revisions after the edge-case review).
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import IO, Any, TextIO

import sandloom.core.cli.checkbox as _checkbox
import sandloom.core.cli.dev.errors as _errors
import sandloom.core.cli.dev.ports as _ports
import sandloom.core.cli.dev.process as _process
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
DOCS_URL = "https://sandloom.dev/docs/"
UV_INSTALL_URL = "https://docs.astral.sh/uv/getting-started/installation/"
#: Where the setup's command output goes (``.sandloom/run/`` is gitignored).
SETUP_LOG = Path(".sandloom/run/new-setup.log")
#: The variables a child ``uv`` must not inherit: they point it at the
#: caller's environment instead of the new app's.
_UV_LEAKS = frozenset({"VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PROJECT"})
DOCKER_TIMEOUT = 10.0
#: A logging line's time, level and logger, cut from the line under the spinner.
_LOG_PREFIX = re.compile(r"^\d{4}-\d\d-\d\d [\d:,.]+ \[\w+\] [\w.]+: ")
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


@dataclass(frozen=True)
class Answers:
    app: str
    batteries: list[str]
    agents: list[str]
    setup: bool
    admin_email: str | None = None


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
    p.add_argument(
        "--framework-req",
        default=None,
        help=(
            "override the framework dependency requirement string "
            f"(default: sandloom[redis,celery]{_project.FRAMEWORK_RANGE}; each "
            "--with module adds its sandloom-<module> package)"
        ),
    )
    p.add_argument(
        "--framework-source",
        choices=("pypi", "git", "path"),
        default=None,
        help="distribution source (checkout: git; installed wheel: pypi)",
    )
    p.add_argument(
        "--framework-path",
        type=Path,
        default=None,
        help="framework repository (holding packages/) for --framework-source path",
    )
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
    )


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
    argv.append("--yes")
    if plan.setup:
        argv.append("--setup")
    return argv


def repeat_line(version: str, plan: Plan, *, cwd: Path) -> str:
    """:func:`repeat_command` as one shell line (the pin in double quotes,
    as the docs write it)."""
    argv = repeat_command(version, plan, cwd=cwd)
    return " ".join(f'"{a}"' if i == 2 else shlex.quote(a) for i, a in enumerate(argv))


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
        name = p.ask(f"App name [{DEFAULT_NAME}]: ") or DEFAULT_NAME
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


def ask_setup(p: Prompter, uv: str | None, docker_ready: Callable[[], bool]) -> bool:
    """Set it up now? Only with uv; with Docker down, the files only."""
    if uv is None:
        p.hint(f"uv is not installed, so the setup is yours to run: {UV_INSTALL_URL}")
        return False
    if not ask_yes(p, "Set it up now?"):
        return False
    while not docker_ready():
        p.say(_ui.error_lines(p.style, "Docker is starting or not running"))
        answer = p.ask("Enter: create the files only, r: check again: ").lower()
        if answer != "r":
            return False
    return True


def ask_admin_email(p: Prompter, default: str | None) -> str | None:
    """The first administrator's email; ``None`` skips the step."""
    if default:
        answer = p.ask(f"First administrator's email [{default}] (- to skip): ")
        if answer == "-":
            return None
        return answer or default
    return p.ask("First administrator's email (Enter to skip): ") or None


def ask(
    p: Prompter,
    given: Given,
    *,
    parent: Path,
    detected: dict[str, str],
    uv: str | None,
    docker_ready: Callable[[], bool],
    git_email: str | None,
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
    setup = given.setup if given.setup else ask_setup(p, uv, docker_ready)
    email = given.admin_email
    if setup and "admin" in batteries and email is None:
        email = ask_admin_email(p, git_email)
    return Answers(app, batteries, agents, setup, email if setup else None)


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


def docker_ready(environ: Mapping[str, str]) -> bool:
    """``docker info`` answers within :data:`DOCKER_TIMEOUT` seconds."""
    proc = _run_quiet(["docker", "info", "--format", "{{.ServerVersion}}"], environ)
    return proc is not None and proc.returncode == 0


def git_email(environ: Mapping[str, str]) -> str | None:
    proc = _run_quiet(["git", "config", "user.email"], environ)
    value = proc.stdout.strip() if proc is not None and proc.returncode == 0 else ""
    return value or None


def git_init(root: Path, environ: Mapping[str, str]) -> bool:
    """``git init`` in ``root`` (no commit), unless ``root`` is already inside
    a work tree or git is missing; whether it ran."""
    probe = root if root.is_dir() else root.parent
    inside = _run_quiet(
        ["git", "rev-parse", "--is-inside-work-tree"], environ, cwd=probe
    )
    if inside is None or inside.stdout.strip() == "true":
        return False
    done = _run_quiet(["git", "init", "--quiet"], environ, cwd=root)
    return done is not None and done.returncode == 0


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


def setup_steps(uv: str, *, admin_email: str | None, attach: bool) -> list[Step]:
    """The setup commands. The administrator is ``admin create``'s business:
    at a terminal it asks for the password itself (``attach``); without
    one it prints a single-use set-password link (``--invite``)."""
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
            steps.append(
                Step(f"uv run {shlex.join(create)}", [uv, "run", *create], True)
            )
        else:
            create.append("--invite")
            steps.append(Step(f"uv run {shlex.join(create)}", [uv, "run", *create]))
    return steps


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
        commands = " && ".join(["cd " + shlex.quote(app_dir), *(s.label for s in rest)])
        head = "Cancelled. Continue with" if cancelled else "Fix it and continue with"
        self.out.write(f"\n{head}: {commands}\n")
        self.out.flush()

    def _step(self, step: Step, *, cwd: Path, env: dict[str, str], log: IO[str]) -> str:
        if step.attached:
            return self._attached(step, cwd=cwd, env=env)
        line = _ui.StepLine(self.style, self.out, step.label, width=self.width).start()
        tail: deque[str] = deque(maxlen=TAIL_LINES)
        errors: list[str] = []
        try:
            proc = self.popen(
                step.argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
            )
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
        started: list[threading.Thread] = []
        try:
            for reader in readers:
                reader.start()
                started.append(reader)
            code, cancelled = self._wait(proc)
        except KeyboardInterrupt:  # Ctrl-C before the wait began
            code, cancelled = self._stop(proc), True
        for reader in started:
            reader.join()
        line.finish(ok=code == 0 and not cancelled)
        if cancelled:
            return "cancelled"
        if code == 0:
            return "ok"
        self._report(tail, errors, code)
        return "failed"

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
                # stderr: what the step says it is doing, under the spinner.
                line.note(_LOG_PREFIX.sub("", text))
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
        for text in tail:
            self.out.write(f"    {self.style.muted(text)}\n")
        reason = errors[-1] if errors else f"exit status {code}"
        self.out.write(_ui.error_lines(self.style, reason) + "\n")
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
    return Given(
        app=args.app,
        batteries=batteries,
        agents=agents,
        setup=True if args.setup else None,
        admin_email=args.admin_email,
    )


def _answers(run: _Run, given: Given) -> Answers | int:
    """Ask at a terminal; elsewhere the flags and the defaults decide."""
    if not run.asking:
        if not given.app:
            return run.fail(
                "the app name is required without a terminal: sandloom new <app> "
                "(e.g. sandloom new shop --yes)",
                2,
            )
        agents = given.agents or _skills.choose_agents(None, None, interactive=False)
        return Answers(
            given.app,
            given.batteries or [],
            agents,
            bool(given.setup),
            given.admin_email,
        )
    width = _ui.columns(run.out)
    for line in _ui.heading(
        run.style,
        run.version,
        "new application",
        width=width,
        height=_ui.rows(run.out),
        mark=True,
    ):
        run.out.write(line + "\n")
    keys = None
    if run.style.motion and _checkbox.available():
        keys = _checkbox.terminal_keys
    prompter = Prompter(run.out, run.style, _reader(run), keys)
    try:
        answers = ask(
            prompter,
            given,
            parent=run.args.dir,
            detected=_skills.detect_agents(),
            uv=uv_binary(run.environ),
            docker_ready=lambda: docker_ready(run.environ),
            git_email=git_email(run.environ),
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


def _next_steps(run: _Run, plan: Plan, root: Path, answers: Answers) -> list[str]:
    """The quickstart lines, or after a setup the ones left to run."""
    in_cwd = plan.parent.resolve() == Path.cwd().resolve()
    lines = _project.quickstart(
        plan.app, plan.module, path=None if in_cwd else str(root)
    )
    if answers.setup:
        port = free_app_port()
        serve = "uv run sandloom dev run" + (f" --port {port}" if port != 8000 else "")
        lines = [lines[0], serve]
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
    if "admin" in answers.batteries and not answers.admin_email:
        run.out.write(
            run.style.muted("  The administrator step is skipped (no --admin-email).")
            + "\n"
        )
    steps = setup_steps(uv, admin_email=answers.admin_email, attach=run.asking)
    runner = SetupRunner(
        out=run.out,
        style=run.style,
        environ=run.environ,
        log=root / SETUP_LOG,
        width=_ui.columns(run.out),
    )
    app_dir = str(root) if root.parent.resolve() != Path.cwd().resolve() else root.name
    return runner.run(steps, cwd=root, app_dir=app_dir)


def _summary(run: _Run, plan: Plan, answers: Answers, next_lines: list[str]) -> str:
    style = run.style
    port = free_app_port()
    url = f"http://127.0.0.1:{port}"
    serve = next_lines[-1].split("#", 1)[0].strip()
    rows: list[tuple[str, str]] = []
    if answers.setup:
        rows.append(("App", f"{style.accent(url)}   {serve}"))
        if "admin" in plan.batteries:
            who = answers.admin_email or "uv run sandloom admin create --email …"
            rows.append(("Console", f"{style.accent(url + '/admin/')}   {who}"))
    rows.append(("Docs", style.accent(DOCS_URL)))
    if answers.setup:
        rows += [
            ("", ""),
            ("Next", " && ".join(line.split("#", 1)[0].strip() for line in next_lines)),
        ]
    footer = [
        style.muted("Same again without questions:"),
        repeat_line(run.version, plan, cwd=Path.cwd()),
    ]
    title = f"{plan.app} is ready" if answers.setup else f"{plan.app} is created"
    return _ui.summary(style, title, rows, footer=footer)


def _finish(run: _Run, plan: Plan, root: Path, answers: Answers) -> int:
    if (run.asking or answers.setup) and git_init(root, run.environ):
        run.out.write(f"  {run.style.ok('git init')}\n")
    if answers.setup:
        result = _setup(run, root, answers)
        if not result.ok:
            return 130 if result.cancelled else 1
        if result.skipped and answers.admin_email:
            email = shlex.quote(answers.admin_email)
            run.out.write(
                run.style.muted(
                    "  No administrator yet. Again: uv run sandloom admin create "
                    f"--email {email} (an account without a password: uv run "
                    f"sandloom admin invite --email {email})"
                )
                + "\n"
            )
            answers = dataclasses.replace(answers, admin_email=None)
    lines = _next_steps(run, plan, root, answers)
    if not run.asking:
        # CI parses this block (tools/smoke/quickstart_check.py): plain mode
        # prints nothing after it.
        run.out.write("\nNext steps:\n" + "".join(f"  {line}\n" for line in lines))
        return 0
    if not answers.setup:
        run.out.write("\nNext steps:\n" + "".join(f"  {line}\n" for line in lines))
    run.out.write(_summary(run, plan, answers, lines))
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
    "Plan",
    "Prompter",
    "Result",
    "SetupRunner",
    "Step",
    "ask",
    "child_env",
    "docker_ready",
    "git_init",
    "main",
    "name_problem",
    "parser",
    "plan_from_args",
    "repeat_command",
    "repeat_line",
    "setup_steps",
    "uv_binary",
]
