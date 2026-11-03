"""The ``sandloom dev`` subcommands (see :mod:`sandloom.core.cli.dev`)."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from dotenv import dotenv_values

import sandloom.core.cli.ui as _ui
from sandloom.core.cli.dev import docker as _docker
from sandloom.core.cli.dev import envfile as _envfile
from sandloom.core.cli.dev import errors as _errors
from sandloom.core.cli.dev import ports as _ports
from sandloom.core.cli.dev import process as _process
from sandloom.core.cli.dev import services as _services
from sandloom.core.cli.dev.identity import COMPOSE_FILE, Identity
from sandloom.core.cli.dev.identity import resolve as resolve_identity
from sandloom.core.cli.dev.locks import FileLock, pid_alive
from sandloom.core.cli.dev.state import (
    CheckoutRow,
    State,
    atomic_write,
    load_registry,
    load_state,
    save_registry,
    save_state,
)
from sandloom.core.testing.machine import sandloom_home

#: A checkout that ran ``dev up`` this recently counts as using the servers.
ACTIVE_SECONDS = 24 * 3600
#: Test databases unused this long (and unlocked) are pruned.
STALE_TEST_DAYS = 3.0
STATE_COMPOSE = "docker-compose.dev.yml"
STATE_ENV = "dev.env"
RUN_FILE = Path("run") / "dev-run.json"
DEFAULT_APP_PORT = 8000
#: Test Redis base indexes in ``.env.test.local`` (each run claims its own).
TEST_CACHE_INDEX, TEST_BROKER_INDEX = 15, 14
#: How long a sticky service port that looks taken is checked again before it
#: moves: Docker Desktop releases a published port a moment after ``down``.
PORT_SETTLE_SECONDS = 5.0
PORT_SETTLE_STEP = 0.25
#: A ``dev down`` this recent means our containers are just stopping: a port
#: Docker's own proxy still holds is ours, not a stranger's.
STOPPING_SECONDS = 120.0
#: Listeners that hold a published port for Docker (Docker Desktop, Colima,
#: OrbStack, Lima, the Linux ``docker-proxy``): command-name prefixes, and
#: ``ssh`` (Colima's and Lima's port forwards).
DOCKER_PORT_HOLDERS = (
    "com.docker.",
    "vpnkit",
    "docker-proxy",
    "orbstack",
    "limactl",
)


class DevError(RuntimeError):
    """A refusal or failure: printed as one ``error:`` line, exit 1."""


@dataclass
class Context:
    identity: Identity
    environ: Mapping[str, str]
    out: TextIO = field(default_factory=lambda: sys.stdout)
    err: TextIO = field(default_factory=lambda: sys.stderr)
    interactive: bool = False
    #: Show Docker Compose's own progress (``--verbose`` / ``--debug``).
    verbose: bool = False
    #: Set by ``up``: this checkout's database is new or holds no table.
    needs_migrate: bool = False

    # --------------------------------------------------------------- paths
    @property
    def home(self) -> Path:
        return sandloom_home(self.environ)

    @property
    def state_dir(self) -> Path:
        """This machine's part of the repository state.

        Keyed by the machine id: a devcontainer or a sandboxed agent with its
        own HOME on the same repository keeps its own compose project, ports
        and checkout rows instead of replacing the other's."""
        return self.identity.state_dir / "machines" / self.identity.machine

    @property
    def root(self) -> Path:
        return self.identity.root

    @property
    def host(self) -> str:
        return _docker.docker_host(self.environ)

    @property
    def style(self) -> _ui.Style:
        """How ``out`` is written (colour only at a terminal; the plain text
        is the same either way)."""
        return _ui.style_for(self.out, self.environ)

    def say(self, text: str) -> None:
        print(text, file=self.out, flush=True)

    def warn(self, text: str) -> None:
        print(f"warning: {text}", file=self.err, flush=True)

    # --------------------------------------------------------------- locks
    def machine_lock(self) -> FileLock:
        return FileLock(
            self.home / "ports.lock",
            waiting="waiting for another sandloom dev command choosing ports",
            out=self.err,
        )

    def repo_lock(self, command: str = "sandloom dev") -> FileLock:
        repository = self.identity.repository
        where = repository.parent if repository.name == ".git" else repository
        return FileLock(
            self.state_dir / "dev.lock",
            command=command,
            waiting=f"waiting for another `{command}` in {where}",
            out=self.err,
        )

    def checkout_lock(self, root: Path) -> FileLock:
        return FileLock(root / ".sandloom" / "checkout.lock", out=self.err)

    # ------------------------------------------------------------- compose
    def compose(self, state: State) -> _docker.Compose:
        return _docker.Compose(
            project=state.project,
            compose_file=self.state_dir / STATE_COMPOSE,
            env_file=self.state_dir / STATE_ENV,
            environ=self.environ,
        )

    def postgres(self, state: State, *, test: bool = False) -> _services.Postgres:
        if test:
            return _services.Postgres(
                self.host, state.ports["postgres_test"], *_services.TEST_CREDENTIALS
            )
        return _services.Postgres(
            self.host, state.ports["db"], *_services.DEV_CREDENTIALS
        )


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def in_ci(environ: Mapping[str, str]) -> bool:
    return _truthy(environ.get("CI"))


def write_state_env(ctx: Context, state: State) -> None:
    lines = [
        "# Written by `sandloom dev up`: the compose project's ports.",
        f"SANDLOOM_DEV_PROJECT={state.project}",
        *(
            f"{var}={state.ports[service]}"
            for service, (var, _) in _ports.SERVICES.items()
            if service in state.ports
        ),
    ]
    atomic_write(ctx.state_dir / STATE_ENV, "\n".join(lines) + "\n")


def checkout_urls(ctx: Context, state: State, row: CheckoutRow) -> dict[str, str]:
    host = ctx.host
    pg = _services.Postgres(host, state.ports["db"], *_services.DEV_CREDENTIALS)
    redis = f"redis://{host}:{state.ports['redis']}"
    return {
        "DATABASE_URL": pg.url(row.database),
        "CACHE_URL": f"{redis}/{row.cache_index}",
        "BROKER_URL": f"{redis}/{row.broker_index}",
    }


def test_urls(ctx: Context, state: State, row: CheckoutRow) -> dict[str, str]:
    host = ctx.host
    pg = _services.Postgres(
        host, state.ports["postgres_test"], *_services.TEST_CREDENTIALS
    )
    redis = f"redis://{host}:{state.ports['redis_test']}"
    return {
        "SANDLOOM_TEST_DATABASE_URL": pg.url(row.test_database),
        "SANDLOOM_TEST_CACHE_URL": f"{redis}/{TEST_CACHE_INDEX}",
        "SANDLOOM_TEST_BROKER_URL": f"{redis}/{TEST_BROKER_INDEX}",
    }


def allocate_indexes(state: State, path: str, *, main: bool) -> tuple[int, int]:
    """The dev Redis pair of a new checkout: (1, 2) for the main checkout when
    free, else the lowest free pair (index 0 stays unused)."""
    used = state.used_indexes(besides=path)
    if main and not {1, 2} & used:
        return (1, 2)
    free = [i for i in range(1, 1024) if i not in used]
    if len(free) < 2:
        raise DevError("the dev Redis has no free database index left (1024)")
    return (free[0], free[1])


def checkout_row(ctx: Context, state: State) -> CheckoutRow:
    key = str(ctx.root)
    row = state.checkouts.get(key)
    if row is None:
        cache, broker = allocate_indexes(state, key, main=ctx.identity.main)
        row = CheckoutRow(
            path=key,
            database=ctx.identity.database,
            cache_index=cache,
            broker_index=broker,
            test_database=ctx.identity.test_database,
        )
        state.checkouts[key] = row
    return row


def write_checkout_files(ctx: Context, state: State, row: CheckoutRow) -> list[str]:
    """``.env`` block and ``.env.test.local`` of ``row`` (call under its lock)."""
    root = Path(row.path)
    notes = []
    if _envfile.seed_env(root):
        notes.append(f"created {root / '.env'} from .env.example (fresh JWT_SECRET)")
    if _envfile.write_env_block(root / ".env", checkout_urls(ctx, state, row)):
        notes.append(f"wrote the sandloom dev block of {root / '.env'}")
    if _envfile.write_test_local(root, test_urls(ctx, state, row)):
        notes.append(f"wrote {root / '.env.test.local'}")
    return notes


def other_active(ctx: Context, state: State, *, now: float | None = None) -> list[str]:
    """Other checkouts that ran ``dev up`` in the last 24 h (and still exist)."""
    now = time.time() if now is None else now
    return sorted(
        path
        for path, row in state.checkouts.items()
        if path != str(ctx.root)
        and Path(path).exists()
        and now - row.last_up < ACTIVE_SECONDS
    )


def run_file(root: Path) -> Path:
    return root / ".sandloom" / RUN_FILE


def live_run(root: Path) -> dict[str, Any] | None:
    """The live ``dev run`` of the checkout at ``root`` (its pid file)."""
    try:
        data = json.loads(run_file(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("host") not in (None, socket.gethostname()):
        return None
    try:
        alive = pid_alive(int(data.get("pid") or 0))
    except (TypeError, ValueError):
        alive = False
    return data if alive else None


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _confirm(ctx: Context, question: str) -> bool:
    if not ctx.interactive:
        return False
    try:
        answer = input(f"{question} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


# --------------------------------------------------------------------------- #
# up
# --------------------------------------------------------------------------- #


@dataclass
class UpOptions:
    recreate: bool = False
    force: bool = False
    adopt: bool = False
    pull: bool = True


def _legacy_name(ctx: Context) -> str | None:
    """An existing ``name: <app>-dev`` project started from this checkout."""
    name = f"{ctx.identity.slug}-dev"
    workdirs = _docker.legacy_projects(name, ctx.environ)
    deploy = str(ctx.root / "deploy")
    return name if deploy in workdirs else None


def reconcile(ctx: Context, state: State | None, options: UpOptions) -> State:
    """``state`` made to fit this repository (new, copied, moved, legacy)."""
    identity = ctx.identity
    fresh = State(
        repository=str(identity.repository),
        project=identity.project,
        machine=identity.machine,
    )
    if state is None:
        legacy = _legacy_name(ctx)
        if legacy and (
            options.adopt
            or _confirm(
                ctx,
                f"containers of the compose project {legacy!r} (from before "
                "`sandloom dev`) hold this app's data; keep using that project?",
            )
        ):
            fresh.project = legacy
            ctx.say(f"adopted the compose project {legacy} (its volumes keep the data)")
        elif legacy:
            ctx.warn(
                f"the old compose project {legacy} still exists; this repository "
                f"now uses {fresh.project}. `uv run sandloom dev up --adopt` (before "
                "the first up) keeps the old data instead"
            )
        return fresh
    if state.repository == str(identity.repository) and state.machine == (
        identity.machine
    ):
        return state
    old = Path(state.repository)
    if old.exists() or state.machine != identity.machine:
        # A copy (`cp -r`) or another machine: a new repository identity.
        ctx.say(
            f"this repository was copied from {old}: it gets its own services "
            f"({fresh.project})"
        )
        return fresh
    if options.adopt or _confirm(
        ctx,
        f"this repository moved from {old}; keep its services and data "
        f"({state.project})?",
    ):
        state.repository = str(identity.repository)
        state.checkouts = {p: r for p, r in state.checkouts.items() if Path(p).exists()}
        ctx.say(f"adopted the services of {old} ({state.project})")
        return state
    ctx.warn(
        f"this repository moved from {old}; it gets new services ({fresh.project}). "
        f"The old project {state.project} keeps its data: `uv run sandloom dev up "
        "--adopt` uses it instead"
    )
    return fresh


def ports_needed(
    ctx: Context, state: State, *, settle: float | None = None
) -> list[str]:
    """Services whose port must be (re)chosen: none yet, or a sticky port
    something else took while that service was down.

    A taken port is checked again for ``settle`` seconds (default
    :data:`PORT_SETTLE_SECONDS`): Docker Desktop releases a published port a
    moment after ``down``. One that Docker's own proxy still holds while our
    containers are just stopping is ours (:func:`_held_for_us`)."""
    missing = [s for s in _ports.SERVICES if s not in state.ports]
    if missing:
        return missing
    write_state_env(ctx, state)
    running: dict[str, str] = {}
    if (ctx.state_dir / STATE_COMPOSE).is_file():
        running = ctx.compose(state).states()
    taken = [
        service
        for service, port in state.ports.items()
        if running.get(service) != "running" and not _ports.port_free(port)
    ]
    wait = PORT_SETTLE_SECONDS if settle is None else settle
    deadline = time.monotonic() + wait
    while taken and time.monotonic() < deadline:
        time.sleep(PORT_SETTLE_STEP)
        taken = [s for s in taken if not _ports.port_free(state.ports[s])]
    return [s for s in taken if not _held_for_us(state, running, state.ports[s])]


def _held_for_us(state: State, running: Mapping[str, str], port: int) -> bool:
    """Whether Docker's own proxy holds ``port`` while this project's
    containers are just stopping (a recent ``dev down``, or a container
    being removed): the port is still ours, not a stranger's."""
    found = port_listener(port)
    if found is None:
        return False
    name = found[0].lower()
    if not (name == "ssh" or name.startswith(DOCKER_PORT_HOLDERS)):
        return False
    stopping = time.time() - state.stopped_at < STOPPING_SECONDS
    return stopping or any(s == "removing" for s in running.values())


def assign_ports(ctx: Context, state: State, services: list[str]) -> bool:
    """Choose ``services``' ports under the machine lock (held by the caller);
    ``True`` when existing ports moved."""
    registry = load_registry(ctx.home)
    moved = bool(state.ports) and any(s in state.ports for s in services)
    keep = {p for s, p in state.ports.items() if s not in services}
    reserved = (
        set(registry) | keep | {state.ports[s] for s in services if s in state.ports}
    )
    chosen = _ports.choose_ports(state.project, services, reserved=reserved)
    for service in services:
        old = state.ports.get(service)
        if old is not None and registry.get(old) == state.repository:
            registry.pop(old, None)
    state.ports.update(chosen)
    for port in state.ports.values():
        registry[port] = state.repository
    save_registry(ctx.home, registry)
    return moved


def registry_stale(ctx: Context, state: State) -> bool:
    """Whether a port of ``state`` is registered to no repository or to one
    that is gone (a moved repository adopted its services): ``dev prune``
    would free it while the services still use it."""
    registry = load_registry(ctx.home)
    for port in state.ports.values():
        owner = registry.get(port)
        if owner != state.repository and (owner is None or not Path(owner).exists()):
            return True
    return False


def reregister_ports(ctx: Context, state: State) -> None:
    """Register ``state``'s ports under its (new) repository path; ports that
    a live repository holds are left alone. Under the machine lock."""
    registry = load_registry(ctx.home)
    for port in state.ports.values():
        owner = registry.get(port)
        if owner is None or not Path(owner).exists():
            registry[port] = state.repository
    save_registry(ctx.home, registry)


@contextlib.contextmanager
def _checkout_locks(ctx: Context, paths: list[str]) -> Iterator[None]:
    with contextlib.ExitStack() as stack:
        for path in sorted(paths):
            if Path(path).is_dir():
                stack.enter_context(ctx.checkout_lock(Path(path)))
        yield


def _pull_missing(ctx: Context) -> None:
    """Pull missing images BEFORE the repository lock (a slow pull must not
    block other checkouts)."""
    source = ctx.root / COMPOSE_FILE
    dummy = {
        **ctx.environ,
        "SANDLOOM_DEV_PROJECT": "pull",
        **{var: "1" for var, _ in _ports.SERVICES.values()},
    }
    proc = _process.run(
        ["docker", "compose", "-f", str(source), "config", "--images"], env=dummy
    )
    images = proc.stdout.split() if proc.returncode == 0 else []
    missing = _docker.missing_images(images, ctx.environ)
    if missing:
        ctx.say(f"pulling {', '.join(missing)}")
        _docker.pull(missing, ctx.environ)


def up(ctx: Context, options: UpOptions) -> int:
    source = ctx.root / COMPOSE_FILE
    if not source.is_file():
        raise DevError(
            f"no {COMPOSE_FILE} in {ctx.root} (`sandloom new` writes it); "
            "run this in an app's checkout"
        )
    _docker.check_docker(ctx.environ)
    if options.pull:
        _pull_missing(ctx)
    for _ in range(3):
        with ctx.repo_lock("sandloom dev up"):
            state = reconcile(ctx, load_state(ctx.state_dir), options)
            if not ports_needed(ctx, state) and not registry_stale(ctx, state):
                return _finish_up(ctx, state, options)
        # Ports: release the repository lock, take machine then repository.
        with ctx.machine_lock(), ctx.repo_lock("sandloom dev up"):
            state = reconcile(ctx, load_state(ctx.state_dir), options)
            needed = ports_needed(ctx, state, settle=0.0)
            if needed:
                moved = assign_ports(ctx, state, needed)
                save_state(ctx.state_dir, state)
                write_state_env(ctx, state)
                if moved:
                    _rewrite_checkouts(ctx, state, needed)
            elif registry_stale(ctx, state):
                reregister_ports(ctx, state)
                save_state(ctx.state_dir, state)
    raise DevError("the ports kept changing while `sandloom dev up` ran; run it again")


def _rewrite_checkouts(ctx: Context, state: State, services: list[str]) -> None:
    paths = [p for p in state.checkouts if Path(p).is_dir()]
    with _checkout_locks(ctx, paths):
        for path in paths:
            write_checkout_files(ctx, state, state.checkouts[path])
    ctx.warn(
        f"{', '.join(services)} moved to a new port (the old one was taken while "
        "the services were down); the .env block and .env.test.local of every "
        "checkout were rewritten. Restart running app servers."
    )


def _apply_compose_file(ctx: Context, state: State, options: UpOptions) -> bool:
    """Bring the CLI's copy of the compose file up to date when that is safe;
    ``True`` when it changed."""
    source = (ctx.root / COMPOSE_FILE).read_text(encoding="utf-8")
    ours = ctx.state_dir / STATE_COMPOSE
    current = ours.read_text(encoding="utf-8") if ours.is_file() else None
    if current == source:
        state.compose_sha = _sha(source)
        return False
    running = (
        any(s == "running" for s in ctx.compose(state).states().values())
        if current is not None
        else False
    )
    if running and not options.recreate:
        ctx.warn(
            f"{COMPOSE_FILE} of this checkout differs from the configuration the "
            "running services use (another branch changed it?); not applied. "
            "`uv run sandloom dev up --recreate` applies it"
        )
        return False
    if running:
        busy = _sessions_of_others(ctx, state)
        if busy and not options.force:
            raise DevError(
                "not recreating the services: other checkouts use them ("
                + "; ".join(busy)
                + "). Add --force to recreate them anyway"
            )
    atomic_write(ours, source)
    state.compose_sha = _sha(source)
    return current is not None  # the first copy is not a change


def _sessions_of_others(ctx: Context, state: State) -> list[str]:
    """Who else is connected (other checkouts' databases, Redis clients)."""
    busy: list[str] = []
    own = state.checkouts.get(str(ctx.root))
    own_db = own.database if own else ctx.identity.database
    with contextlib.suppress(Exception):
        counts = _services.sessions(ctx.postgres(state))
        busy += [f"{n} session(s) on {db}" for db, n in counts.items() if db != own_db]
    with contextlib.suppress(Exception):
        tests = _services.sessions(ctx.postgres(state, test=True))
        busy += [f"{n} session(s) on test database {db}" for db, n in tests.items()]
    with contextlib.suppress(Exception):
        others = {
            i
            for path, row in state.checkouts.items()
            if path != str(ctx.root)
            for i in row.indexes
        }
        if others:
            n = _services.redis_clients(ctx.host, state.ports["redis"], others)
            if n:
                busy.append(f"{n} Redis client(s) of other checkouts")
    with contextlib.suppress(Exception):
        n = _services.redis_clients(ctx.host, state.ports["redis_test"])
        if n:
            busy.append(f"{n} test Redis client(s)")
    return busy


def _probe(ctx: Context, state: State, service: str) -> str | None:
    """``None`` when ``service`` answers from the host, else why not."""
    _, container_port = _ports.SERVICES[service]
    port = state.ports[service]
    published = ctx.compose(state).published_port(service, container_port)
    if published is None:
        return "the port is not published"
    if published != port:
        return f"published on {published}, expected {port}"
    if service in ("db", "postgres_test"):
        return _services.pg_ready(ctx.postgres(state, test=service == "postgres_test"))
    return _services.redis_ready(ctx.host, port)


def _ensure_reachable(ctx: Context, state: State, options: UpOptions) -> None:
    for service in _ports.SERVICES:
        problem = None
        for _ in range(20):  # the loopback forward can land after "healthy"
            problem = _probe(ctx, state, service)
            if problem is None:
                break
            time.sleep(0.25)
        if problem is None:
            continue
        port = state.ports[service]
        others = other_active(ctx, state)
        if (
            others
            and not options.force
            and not _confirm(
                ctx,
                f"{service} is healthy in Docker but not reachable at "
                f"{ctx.host}:{port} ({problem}); recreate it although "
                f"{len(others)} other checkout(s) use the services?",
            )
        ):
            raise DevError(
                f"{service} is healthy in Docker but not reachable at "
                f"{ctx.host}:{port} ({problem}); other checkouts use the services "
                f"({', '.join(others)}), so it was not recreated. `uv run sandloom "
                "dev up --force` recreates it"
            )
        ctx.say(
            f"{service} is not reachable at {ctx.host}:{port} ({problem}); "
            "recreating it"
        )
        ctx.compose(state).up([service], force_recreate=True)
        for _ in range(20):
            problem = _probe(ctx, state, service)
            if problem is None:
                break
            time.sleep(0.25)
        if problem is not None:
            raise DevError(
                f"{service} is still not reachable at {ctx.host}:{port} ({problem}) "
                "after recreating it. Restart Docker, then run "
                "`uv run sandloom dev up` again"
            )


def _finish_up(ctx: Context, state: State, options: UpOptions) -> int:
    changed = _apply_compose_file(ctx, state, options)
    write_state_env(ctx, state)
    save_state(ctx.state_dir, state)
    proc = ctx.compose(state).up()
    if proc.returncode != 0:
        detail = ((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()
        raise DevError(
            "`docker compose up` failed"
            + (f": {detail[-1]}" if detail else "")
            + f" (project {state.project}; `uv run sandloom dev logs` shows the logs)"
        )
    if changed:
        ctx.say(f"applied {COMPOSE_FILE} to the services")
    _ensure_reachable(ctx, state, options)
    with ctx.checkout_lock(ctx.root):
        row = checkout_row(ctx, state)
        pg = ctx.postgres(state)
        created = _services.ensure_database(pg, row.database)
        if created:
            ctx.say(f"created the database {row.database}")
        notes = write_checkout_files(ctx, state, row)
        ensure_app_port(ctx, state)
        row.last_up = time.time()
        save_state(ctx.state_dir, state)
    for note in notes:
        ctx.say(note)
    migrate = created
    if not migrate:
        with contextlib.suppress(*_services.service_errors()):
            migrate = not _services.has_tables(pg, row.database)
    ctx.needs_migrate = migrate
    _print_summary(ctx, state, row, migrate=migrate)
    return 0


def _print_summary(
    ctx: Context, state: State, row: CheckoutRow, *, migrate: bool
) -> None:
    """The URLs, then the next command: the migrations only for a database
    that was just created or holds no table yet."""
    urls = checkout_urls(ctx, state, row)
    tests = test_urls(ctx, state, row)
    gui = ctx.postgres(state).url(row.database, driver="postgresql")
    port = state.app_ports.get(row.path)
    serve = "uv run sandloom dev run" + (f" (http://127.0.0.1:{port})" if port else "")
    following = (
        f"next: uv run sandloom migrate upgrade-all --apply, then {serve}"
        if migrate
        else f"next: {serve}"
    )
    ctx.say(
        "\n".join(
            [
                ctx.style.green(f"dev services up (compose project {state.project})"),
                f"  app      DATABASE_URL={urls['DATABASE_URL']}",
                f"           CACHE_URL={urls['CACHE_URL']}",
                f"           BROKER_URL={urls['BROKER_URL']}",
                "  tests    SANDLOOM_TEST_DATABASE_URL="
                + tests["SANDLOOM_TEST_DATABASE_URL"],
                f"  GUI      {gui}",
                following,
            ]
        )
    )


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


def status_data(ctx: Context) -> dict[str, Any]:
    state = load_state(ctx.state_dir)
    data: dict[str, Any] = {
        "repository": str(ctx.identity.repository),
        "checkout": str(ctx.root),
        "state": str(ctx.state_dir),
        "up": False,
    }
    if state is None or len(state.ports) < len(_ports.SERVICES):
        data["hint"] = "run `uv run sandloom dev up`"
        return data
    data["project"] = state.project
    states: dict[str, str] = {}
    with contextlib.suppress(Exception):
        if (ctx.state_dir / STATE_COMPOSE).is_file():
            write_state_env(ctx, state)
            states = ctx.compose(state).states()
    services = {}
    for service, port in state.ports.items():
        running = states.get(service, "absent")
        problem = _probe(ctx, state, service) if running == "running" else running
        services[service] = {
            "port": port,
            "state": running,
            "reachable": problem is None,
            **({"problem": problem} if problem else {}),
        }
    data["services"] = services
    data["up"] = all(s["reachable"] for s in services.values())
    row = state.checkouts.get(str(ctx.root))
    if row is not None:
        data["database"] = row.database
        data["redis_indexes"] = list(row.indexes)
        data["urls"] = checkout_urls(ctx, state, row)
        data["test_urls"] = test_urls(ctx, state, row)
        data["gui_url"] = ctx.postgres(state).url(row.database, driver="postgresql")
        run = live_run(ctx.root)
        if run:
            data["run"] = {"pid": run.get("pid"), "url": run.get("url")}
        try:
            block = _envfile.read_block((ctx.root / ".env").read_text(encoding="utf-8"))
        except OSError:
            block = {}
        drift = _envfile.exported_drift(block, dict(ctx.environ))
        if drift:
            data["exported_overrides"] = drift
    else:
        data["hint"] = "this checkout has no database yet: run `uv run sandloom dev up`"
    data["other_checkouts"] = [
        {
            "path": path,
            "database": r.database,
            "last_up": r.last_up,
            "exists": Path(path).exists(),
        }
        for path, r in sorted(state.checkouts.items())
        if path != str(ctx.root)
    ]
    return data


def status(ctx: Context, *, as_json: bool) -> int:
    data = status_data(ctx)
    if as_json:
        ctx.say(json.dumps(data, indent=2, sort_keys=True))
        return 0
    if "project" not in data:
        ctx.say(f"no dev services for this repository yet: {data.get('hint')}")
        return 0
    lines = [f"compose project {data['project']} ({data['repository']})"]
    for name, svc in data["services"].items():
        mark = (
            ctx.style.green("ok")
            if svc["reachable"]
            else ctx.style.red(f"NOT reachable: {svc.get('problem')}")
        )
        lines.append(f"  {name:14} {ctx.host}:{svc['port']:<6} {svc['state']:9} {mark}")
    if "urls" in data:
        lines.append(f"this checkout ({data['checkout']}): database {data['database']}")
        for key, value in data["urls"].items():
            lines.append(f"  {key}={value}")
        lines.append(f"  GUI: {data['gui_url']}")
        if "run" in data:
            lines.append(f"  dev run: {data['run']['url']} (pid {data['run']['pid']})")
    else:
        lines.append(data.get("hint", ""))
    lines.extend(
        f"warning: {key} is exported in this shell and differs from .env: "
        "the exported value wins"
        for key in data.get("exported_overrides", [])
    )
    others = data["other_checkouts"]
    if others:
        lines.append("other checkouts on these services:")
        now = time.time()
        for other in others:
            age = (now - other["last_up"]) / 3600
            gone = "" if other["exists"] else " (gone: `uv run sandloom dev prune`)"
            lines.append(
                f"  {other['path']}  {other['database']}  up {age:.0f}h ago{gone}"
            )
    ctx.say("\n".join(lines))
    return 0


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


def port_listener(port: int) -> tuple[str, int] | None:
    """The command name and pid listening on ``port`` (``lsof``), when the
    tool exists and tells."""
    if _process.which("lsof") is None:
        return None
    try:
        proc = _process.run(
            ["lsof", "+c", "0", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            env=_errors.environment(),
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    rows = proc.stdout.strip().splitlines()[1:]
    if not rows:
        return None
    parts = rows[0].split()
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    # lsof escapes a space in the command name (`OrbStack\x20Helper`).
    return parts[0].replace("\\x20", " "), int(parts[1])


def port_holder(port: int) -> str | None:
    """Who listens on ``port``, for a message (``lsof``)."""
    found = port_listener(port)
    return f"{found[0]} (pid {found[1]})" if found else None


def free_port(
    preferred: int = DEFAULT_APP_PORT + 1,
    *,
    skip: Collection[int] = (),
    is_free: Callable[[int], bool] | None = None,
) -> int:
    check = _ports.port_free if is_free is None else is_free
    for port in range(preferred, preferred + 200):
        if port not in skip and check(port):
            return port
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def app_target(root: Path, app: str, environ: Mapping[str, str]) -> str:
    composition = (environ.get("SANDLOOM_COMPOSITION_ROOT") or "").strip()
    if not composition:
        values = dotenv_values(root / ".env") if (root / ".env").is_file() else {}
        composition = (values.get("SANDLOOM_COMPOSITION_ROOT") or "").strip()
    composition = composition or f"{app}.asgi"
    return composition if ":" in composition else f"{composition}:app"


def ensure_app_port(
    ctx: Context, state: State, *, is_free: Callable[[int], bool] | None = None
) -> int:
    """This checkout's app port, chosen once and kept in the state: 8000
    when free, else the next free one; never another checkout's. The
    caller holds the repository lock and saves the state."""
    key = str(ctx.root)
    port = state.app_ports.get(key)
    if port:
        return port
    siblings = {p for k, p in state.app_ports.items() if k != key}
    taken = siblings | set(state.ports.values())
    check = _ports.port_free if is_free is None else is_free
    port = free_port(DEFAULT_APP_PORT, skip=taken, is_free=check)
    if not check(port):  # nothing free near 8000: keep 8000, dev run says why
        port = DEFAULT_APP_PORT
    state.app_ports[key] = port
    return port


def app_url(root: Path, environ: Mapping[str, str]) -> str | None:
    """Where this checkout's app answers: its live ``dev run`` (the run
    file), else its sticky app port (``dev up``); ``None`` when neither is
    known. ``sandloom admin create`` / ``invite`` build their links on it."""
    live = live_run(root)
    if live is not None:
        url = live.get("url")
        if isinstance(url, str) and url.startswith("http"):
            return url.rstrip("/")
        port = live.get("port")
        if isinstance(port, int) and 0 < port < 65536:
            return f"http://127.0.0.1:{port}"
    try:
        ctx = Context(identity=resolve_identity(root, environ), environ=environ)
    except (OSError, ValueError):
        return None
    state = load_state(ctx.state_dir)
    port = state.app_ports.get(str(ctx.root)) if state is not None else None
    return f"http://127.0.0.1:{port}" if port else None


def _sticky_app_port(ctx: Context) -> int | None:
    """This checkout's app port; chosen now (and saved) for a state from
    before app ports, ``None`` without a state."""
    with ctx.repo_lock("sandloom dev run"):
        state = load_state(ctx.state_dir)
        if state is None:
            return None
        known = state.app_ports.get(str(ctx.root))
        if known or str(ctx.root) not in state.checkouts:
            return known
        port = ensure_app_port(ctx, state)
        save_state(ctx.state_dir, state)
        return port


def app_port(port: str | None, *, sticky: int | None = None) -> int:
    """The port ``dev run`` serves on: ``port``, else the checkout's sticky
    one, else 8000 (``DevError`` naming the holder when it is taken)."""
    if port == "auto":
        return free_port(DEFAULT_APP_PORT)
    if port in (None, ""):
        number = sticky or DEFAULT_APP_PORT
    else:
        try:
            number = int(str(port))
        except ValueError as exc:
            raise DevError(f"--port takes a number or `auto`, not {port!r}") from exc
    if _ports.port_free(number):
        return number
    holder = port_holder(number)
    suggestion = free_port(number + 1)
    raise DevError(
        f"port {number} is in use"
        + (f" by {holder}" if holder else "")
        + f"; port {suggestion} is free: uv run sandloom dev run --port "
        f"{suggestion} (or --port auto)"
    )


def _read_block(root: Path) -> dict[str, str]:
    try:
        return _envfile.read_block((root / ".env").read_text(encoding="utf-8"))
    except OSError:
        return {}


def services_reachable(ctx: Context) -> bool:
    """Whether this checkout's dev PostgreSQL and Redis answer from the host
    (``False`` without a state: no ``dev up`` yet)."""
    state = load_state(ctx.state_dir)
    if state is None or len(state.ports) < len(_ports.SERVICES):
        return False
    if state.checkouts.get(str(ctx.root)) is None:
        return False
    return (
        _services.pg_ready(ctx.postgres(state)) is None
        and _services.redis_ready(ctx.host, state.ports["redis"]) is None
    )


def _start_services(ctx: Context) -> None:
    """``dev run`` on stopped (or never started) services: ``dev up`` with
    its locks and checks, in one line; its failure is its own one line."""
    ctx.say("starting the dev services…")
    quiet = Context(
        identity=ctx.identity,
        environ=ctx.environ,
        out=io.StringIO(),
        err=ctx.err,
        interactive=ctx.interactive,
        verbose=ctx.verbose,
    )
    up(quiet, UpOptions())
    if quiet.needs_migrate:
        ctx.warn(
            "the database has no tables yet: uv run sandloom migrate upgrade-all "
            "--apply"
        )


def run(ctx: Context, *, port: str | None, reload: bool) -> int:
    """Serve this checkout's app. The person's "run my app": stopped dev
    services are started first (unlike every other command), so the app
    never boots without its database. Not in CI, and not for a checkout
    that points ``DATABASE_URL`` at its own server instead of ``dev up``."""
    block = _read_block(ctx.root)
    own_services = bool(block) or not ctx.environ.get("DATABASE_URL")
    if (
        own_services
        and not in_ci(ctx.environ)
        and not live_run(ctx.root)
        and (not block or not services_reachable(ctx))
    ):
        _start_services(ctx)
        block = _read_block(ctx.root)
    if not block and not ctx.environ.get("DATABASE_URL"):
        raise DevError(
            "this checkout has no dev services yet (the sandloom dev block of .env "
            "is empty): run `uv run sandloom dev up` first"
        )
    marker = run_file(ctx.root)
    existing = live_run(ctx.root)
    if existing:
        ctx.say(f"already running at {existing.get('url')} (pid {existing.get('pid')})")
        return 0
    sticky = _sticky_app_port(ctx) if port in (None, "") else None
    with ctx.checkout_lock(ctx.root):
        existing = live_run(ctx.root)
        if existing:
            ctx.say(
                f"already running at {existing.get('url')} (pid {existing.get('pid')})"
            )
            return 0
        number = app_port(port, sticky=sticky)
        url = f"http://127.0.0.1:{number}"
        record = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "port": number,
            "url": url,
            "started": time.time(),
        }
        atomic_write(marker, json.dumps(record) + "\n")
    env = dict(ctx.environ)
    # The checkout's services, explicitly: an exported URL would beat .env.
    for key in _envfile.exported_drift(block, env):
        ctx.warn(f"{key} is exported in this shell; dev run uses this checkout's block")
    env.update(block)
    argv = [
        sys.executable,
        "-m",
        "uvicorn",
        app_target(ctx.root, ctx.identity.app, ctx.environ),
        "--host",
        "127.0.0.1",
        "--port",
        str(number),
        *(["--reload"] if reload else []),
    ]
    ctx.say(f"sandloom dev run: {ctx.style.accent(url)} (health: {url}/health)")
    try:
        return _serve(ctx, argv, env)
    finally:
        with contextlib.suppress(OSError, ValueError):
            data = json.loads(marker.read_text(encoding="utf-8"))
            if data.get("pid") == os.getpid():
                marker.unlink()


def _serve(ctx: Context, argv: list[str], env: dict[str, str]) -> int:
    child = _process.spawn(argv, cwd=ctx.root, env=env)

    def _forward(signum: int, _frame: object) -> None:
        with contextlib.suppress(ProcessLookupError):
            child.send_signal(signum)

    previous = signal.signal(signal.SIGTERM, _forward)
    try:
        return int(child.wait())
    except KeyboardInterrupt:
        # Ctrl-C is how a person stops the server: a clean stop, exit 0.
        with contextlib.suppress(ProcessLookupError):
            child.send_signal(signal.SIGINT)
        try:
            child.wait(timeout=20)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            child.kill()
            child.wait()
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous)


# --------------------------------------------------------------------------- #
# reset / down / prune / logs
# --------------------------------------------------------------------------- #


def _own_users(ctx: Context, state: State, row: CheckoutRow) -> list[str]:
    users = []
    with contextlib.suppress(Exception):
        n = _services.sessions(ctx.postgres(state), [row.database]).get(row.database, 0)
        if n:
            users.append(f"{n} session(s) on {row.database}")
    run_ = live_run(Path(row.path))
    if run_:
        users.append(f"`sandloom dev run` at {run_.get('url')} (pid {run_.get('pid')})")
    return users


def _require_state(ctx: Context) -> State:
    state = load_state(ctx.state_dir)
    if state is None or len(state.ports) < len(_ports.SERVICES):
        raise DevError(
            "no dev services for this repository: run `uv run sandloom dev up`"
        )
    return state


def reset(ctx: Context, *, force: bool) -> int:
    with ctx.repo_lock("sandloom dev reset"), ctx.checkout_lock(ctx.root):
        state = _require_state(ctx)
        row = state.checkouts.get(str(ctx.root))
        if row is None:
            raise DevError(
                "this checkout has no database yet: run `uv run sandloom dev up`"
            )
        users = _own_users(ctx, state, row)
        if users and not force:
            raise DevError(
                f"not resetting {row.database}: in use ({'; '.join(users)}). "
                "Stop them, or add --force"
            )
        pg = ctx.postgres(state)
        _services.drop_database(pg, row.database)
        _services.ensure_database(pg, row.database)
        _services.flush_indexes(ctx.host, state.ports["redis"], row.indexes)
    ctx.say(
        f"reset {row.database} and Redis indexes {row.cache_index}, "
        f"{row.broker_index}; next: uv run sandloom migrate upgrade-all --apply"
    )
    return 0


def _confirm_volumes(ctx: Context, state: State, *, force: bool) -> None:
    if ctx.interactive:
        try:
            typed = input(
                f"This deletes every database of {state.project} (all checkouts). "
                f"Type the project name to confirm: "
            )
        except EOFError:
            typed = ""
        if typed.strip() != state.project:
            raise DevError("not confirmed; nothing was deleted")
        return
    if not force:
        raise DevError(
            "--volumes deletes every checkout's data: confirm at a terminal, or add "
            "--force"
        )


def _down_refusal(ctx: Context, state: State, *, all_: bool, force: bool) -> str | None:
    """Why ``down`` must not stop the services now (``None``: it may)."""
    if not all_:
        reasons = []
        others = other_active(ctx, state)
        if others:
            reasons.append(
                "other checkouts used the services in the last 24 h: "
                + ", ".join(others)
            )
        with contextlib.suppress(Exception):
            held = _services.advisory_holders(ctx.postgres(state, test=True))
            if held:
                reasons.append(f"{held} test run(s) hold a test database")
        if reasons:
            return (
                "not stopping the services: "
                + "; ".join(reasons)
                + ". `uv run sandloom dev down --all` stops them anyway"
            )
    if force:
        return None
    rows = (
        list(state.checkouts.values())
        if all_
        else [r for p, r in state.checkouts.items() if p == str(ctx.root)]
    )
    users = [u for row in rows for u in _own_users(ctx, state, row)]
    if users:
        return (
            f"not stopping the services: in use ({'; '.join(users)}). Stop them, "
            "or add --force"
        )
    return None


def _forget(ctx: Context, state: State) -> None:
    """After ``down --volumes``: no ports, no state (the data is gone)."""
    registry = load_registry(ctx.home)
    for port, owner in list(registry.items()):
        if owner == state.repository:
            registry.pop(port)
    save_registry(ctx.home, registry)
    for name in ("dev.json", STATE_ENV, STATE_COMPOSE):
        with contextlib.suppress(FileNotFoundError):
            (ctx.state_dir / name).unlink()


def down(ctx: Context, *, all_: bool, volumes: bool, force: bool) -> int:
    if volumes and not all_:
        raise DevError("--volumes deletes every checkout's data: it needs --all")
    machine = ctx.machine_lock() if volumes else contextlib.nullcontext()
    with machine, ctx.repo_lock("sandloom dev down"):
        state = _require_state(ctx)
        refusal = _down_refusal(ctx, state, all_=all_, force=force)
        if refusal:
            raise DevError(refusal)
        if volumes:
            _confirm_volumes(ctx, state, force=force)
        write_state_env(ctx, state)
        args = ["down", *(["--volumes"] if volumes else [])]
        # Compose's own progress ("Container … Removing") only when asked.
        proc = ctx.compose(state).run(*args, capture=not ctx.verbose)
        if proc.returncode != 0:
            detail = ((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()
            raise DevError(
                f"`docker compose down` failed (exit {proc.returncode})"
                + (f": {detail[-1]}" if detail else "")
                + " (`uv run sandloom dev down --verbose` shows its output)"
            )
        if volumes:
            _forget(ctx, state)
        else:
            state.stopped_at = time.time()
            save_state(ctx.state_dir, state)
    ctx.say(
        f"stopped {state.project}"
        + (" and deleted its data" if volumes else " (the data stays)")
    )
    return 0


def prune(ctx: Context) -> int:
    removed: list[str] = []
    with ctx.machine_lock():
        registry = load_registry(ctx.home)
        stale_ports = [p for p, owner in registry.items() if not Path(owner).exists()]
        for port in stale_ports:
            registry.pop(port)
        if stale_ports:
            save_registry(ctx.home, registry)
            removed.append(f"{len(stale_ports)} port(s) of repositories that are gone")
        with ctx.repo_lock("sandloom dev prune"):
            state = load_state(ctx.state_dir)
            if state is not None and len(state.ports) == len(_ports.SERVICES):
                removed += _prune_checkouts(ctx, state)
                removed += _prune_test_databases(ctx, state)
                save_state(ctx.state_dir, state)
    ctx.say("pruned: " + ("; ".join(removed) if removed else "nothing"))
    return 0


def _prune_checkouts(ctx: Context, state: State) -> list[str]:
    out = []
    for path, row in list(state.checkouts.items()):
        if Path(path).exists():
            continue
        try:
            _services.drop_database(ctx.postgres(state), row.database)
            _services.flush_indexes(ctx.host, state.ports["redis"], row.indexes)
        except _services.service_errors() as exc:  # the services are down
            ctx.warn(f"kept {row.database} of {path}: the services are down ({exc})")
            continue
        del state.checkouts[path]
        out.append(f"database {row.database} of the removed checkout {path}")
    return out


def _prune_test_databases(ctx: Context, state: State) -> list[str]:
    from sandloom.core.testing import resources  # noqa: PLC0415 - optional: the redis extra (resources imports redis at import)

    url = ctx.postgres(state, test=True).url("postgres", driver="postgresql")
    try:
        found = resources.scan_databases(url)
    except _services.service_errors() as exc:  # the test server is down
        ctx.warn(f"test databases not pruned: the test server is down ({exc})")
        return []
    now = time.time()
    out = []
    for resource in found:
        if not resource.name.startswith("sandloom_test_"):
            continue
        prune_it, _ = resources.verdict(resource, now=now, max_age_days=STALE_TEST_DAYS)
        if prune_it and resources.prune(url, resource):
            out.append(f"test database {resource.name}")
    return out


def logs(ctx: Context, *, service: str | None, follow: bool) -> int:
    state = _require_state(ctx)
    write_state_env(ctx, state)
    args = ["logs", *(["-f"] if follow else []), *([service] if service else [])]
    return ctx.compose(state).run(*args).returncode


__all__ = [
    "Context",
    "DevError",
    "UpOptions",
    "allocate_indexes",
    "app_port",
    "app_url",
    "assign_ports",
    "checkout_urls",
    "down",
    "ensure_app_port",
    "in_ci",
    "logs",
    "other_active",
    "port_listener",
    "ports_needed",
    "prune",
    "reconcile",
    "registry_stale",
    "reregister_ports",
    "reset",
    "run",
    "services_reachable",
    "status",
    "status_data",
    "test_urls",
    "up",
    "write_checkout_files",
]
