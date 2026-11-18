"""``sandloom dev`` with PostgreSQL and the keyed server the person already
runs (Homebrew, Postgres.app): no Docker call, the same ``.env`` block and
``.env.test.local``; and the banner ``dev run`` prints once the app is up.

The end-to-end test points "external" at this run's own test PostgreSQL (it
stands in for a Homebrew install) and works in a scratch database it drops;
the keyed server is the in-memory stand-in of
:mod:`sandloom.core.tests._fake_stack` (the real one:
``packages/sandloom-redis/tests/test_dev_external_redis.py``).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest

from sandloom.core.cli import dev as dev_cli
from sandloom.core.cli import stack_service, ui
from sandloom.core.cli.dev import commands, envfile, identity, services
from sandloom.core.cli.dev.state import CheckoutRow, load_state, save_state
from sandloom.core.tests._fake_stack import FakeStack


def _git(cwd: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


@pytest.fixture(autouse=True)
def _no_real_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SANDLOOM_HOME", str(tmp_path / "home"))


#: The composition a generated app selects (``DEFAULT_INFRA`` of ``sandloom new``).
_SELECTION = {
    "database": {"default": {"driver": "sqlalchemy"}},
    "cache": {"default": {"driver": "redis"}},
    "eventbus": {"default": {"driver": "streams"}},
    "broadcast": {"default": {"driver": "redis"}},
    "taskqueue": {"default": {"driver": "celery"}},
    "atomic_store": {"security": {"driver": "redis"}},
    "lease": {"beat": {"driver": "redis"}},
}
#: What the installed drivers declare (``InfraProvider.url_option``).
_URL_OPTIONS = {
    "cache.redis": "url",
    "eventbus.streams": "url",
    "broadcast.redis": "url",
    "taskqueue.celery": "broker",
    "atomic_store.redis": "url",
    "lease.redis": "url",
}


@pytest.fixture(autouse=True)
def _composition(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test apps are not importable packages: give ``dev up`` the
    composition a generated app has."""
    monkeypatch.setattr(
        commands,
        "composition_selection",
        lambda ctx, text, checkout=None: commands.CompositionSelection(
            _SELECTION, _URL_OPTIONS
        ),
    )


@pytest.fixture
def environ(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in {"CI", "DOCKER_HOST"}}
    env["SANDLOOM_HOME"] = str(tmp_path / "home")
    return env


@pytest.fixture
def no_docker(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every Docker entry point fails the test: external mode makes none."""
    called: list[str] = []

    def refuse(name: str) -> Any:
        def call(*_a: Any, **_k: Any) -> Any:
            called.append(name)
            raise AssertionError(f"docker called: {name}")

        return call

    for name in ("check_docker", "missing_images", "pull", "legacy_projects"):
        monkeypatch.setattr(commands._docker, name, refuse(name))
    monkeypatch.setattr(commands._docker.Compose, "up", refuse("compose up"))
    monkeypatch.setattr(commands._docker.Compose, "run", refuse("compose run"))
    monkeypatch.setattr(commands._docker.Compose, "states", refuse("compose ps"))
    return called


def _app(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / "deploy").mkdir(parents=True)
    (root / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n')
    (root / ".env.example").write_text(
        "ENVIRONMENT=dev\nJWT_SECRET=change_me\n" + envfile.empty_block("Redis")
    )
    (root / ".env").write_text("JWT_SECRET=x\n" + envfile.empty_block("Redis"))
    (root / "deploy" / "docker-compose.dev.yml").write_text("services: {}\n")
    _git(root, "init", "-q", "-b", "main")
    return root


def _ctx(root: Path, environ: dict[str, str]) -> commands.Context:
    return commands.Context(
        identity=identity.resolve(root, environ),
        environ=environ,
        out=io.StringIO(),
        err=io.StringIO(),
    )


def _servers() -> tuple[str, str]:
    """This run's own test servers, as an "external" PostgreSQL and Redis."""
    base = os.environ.get("SANDLOOM_TEST_DATABASE_URL", "")
    cache = os.environ.get("SANDLOOM_TEST_CACHE_URL", "")
    if not base or not cache:
        pytest.skip("SANDLOOM_TEST_DATABASE_URL / SANDLOOM_TEST_CACHE_URL not set")
    parts = urlsplit(base)
    database = urlunsplit(("postgresql", parts.netloc, "/postgres", "", ""))
    redis_parts = urlsplit(cache)
    redis = urlunsplit(("redis", redis_parts.netloc, "", "", ""))
    pg = services.Postgres.from_url(database)
    if services.pg_ready(pg) is not None:
        pytest.skip("the test PostgreSQL does not answer")
    return database, redis


# ------------------------------------------------------------- end to end


def test_external_services_end_to_end(
    tmp_path: Path, environ: dict[str, str], no_docker: list[str]
) -> None:
    database, redis = _servers()
    name = f"ux_ext_{uuid.uuid4().hex[:8]}"
    root = _app(tmp_path, name)
    ctx = _ctx(root, environ)
    pg = services.Postgres.from_url(database)
    try:
        options = commands.UpOptions(
            services="external", database_url=database, kv_url=redis
        )
        assert commands.up(ctx, options) == 0
        assert services.database_exists(pg, name)
        state = load_state(ctx.state_dir)
        assert state is not None and state.services == "external"
        # It holds the servers' URLs with their passwords: owner only.
        assert (ctx.state_dir / "dev.json").stat().st_mode & 0o777 == 0o600
        assert state.ports == {}
        block = envfile.read_block((root / ".env").read_text())
        assert block["DATABASE_URL"].endswith(f"@{pg.host}:{pg.port}/{name}")
        infra = json.loads(block["INFRA"])
        assert infra["cache"]["default"]["url"].startswith(redis + "/")
        assert infra["lease"]["beat"]["url"].startswith(redis + "/")
        # What must survive is never on the cache's database.
        assert infra["lease"]["beat"]["url"] != infra["cache"]["default"]["url"]
        test_local = (root / ".env.test.local").read_text()
        assert f"{pg.host}:{pg.port}/sandloom_test_" in test_local
        assert "your own PostgreSQL" in ctx.out.getvalue()  # type: ignore[attr-defined]

        # The choice persists: a plain `dev up` again, no flag, no Docker.
        again = _ctx(root, environ)
        assert commands.up(again, commands.UpOptions()) == 0

        data = commands.status_data(_ctx(root, environ))
        assert data["services_mode"] == "external" and data["up"]
        assert data["database"] == name

        quiet = _ctx(root, environ)
        assert commands.down(quiet, all_=False, volumes=False, force=False) == 0
        assert "nothing to stop" in quiet.out.getvalue()  # type: ignore[attr-defined]

        served: list[list[str]] = []
        runner = _ctx(root, environ)
        commands_serve = commands._serve
        try:
            commands._serve = lambda c, argv, env, **kw: served.append(argv) or 0  # type: ignore[assignment]
            assert commands.run(runner, port="auto", reload=False) == 0
        finally:
            commands._serve = commands_serve  # type: ignore[assignment]
        assert served and no_docker == []
    finally:
        services.drop_database(pg, name)


def _sql(pg: services.Postgres, statement: str, database: str = "postgres") -> None:
    conn = pg.connect(database)
    try:
        with conn.cursor() as cur:
            cur.execute(statement)
    finally:
        conn.close()


def test_external_up_never_adopts_a_database_it_did_not_create(
    tmp_path: Path, environ: dict[str, str], no_docker: list[str]
) -> None:
    """Another project's ``shop`` on the same server: refused, left intact."""
    database, redis = _servers()
    name = f"ux_ext_{uuid.uuid4().hex[:8]}"
    pg = services.Postgres.from_url(database)
    services.ensure_database(pg, name)
    try:
        _sql(pg, "CREATE TABLE theirs (id int)", name)
        root = _app(tmp_path, name)
        options = commands.UpOptions(
            services="external", database_url=database, kv_url=redis
        )
        with pytest.raises(commands.DevError) as refused:
            commands.up(_ctx(root, environ), options)
        message = str(refused.value)
        assert "\n" not in message
        assert f"already has a database {name}" in message
        assert "--database-url" in message
        assert services.has_tables(pg, name)
        assert services.database_mark(pg, name) is None
        block = envfile.read_block((root / ".env").read_text())
        assert not block.get("DATABASE_URL")
    finally:
        services.drop_database(pg, name)


def test_external_reset_and_prune_drop_only_a_marked_database(
    tmp_path: Path,
    environ: dict[str, str],
    no_docker: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The test server is shared by this run's other databases: prune only rows.
    monkeypatch.setattr(commands, "_prune_test_databases", lambda ctx, state: [])
    database, redis = _servers()
    name = f"ux_ext_{uuid.uuid4().hex[:8]}"
    foreign = f"{name}_theirs"
    root = _app(tmp_path, name)
    pg = services.Postgres.from_url(database)
    try:
        options = commands.UpOptions(
            services="external", database_url=database, kv_url=redis
        )
        assert commands.up(_ctx(root, environ), options) == 0
        state = load_state(_ctx(root, environ).state_dir)
        assert state is not None
        row = state.checkouts[str(root)]
        assert services.database_mark(pg, name) == commands.dev_mark(row)

        # A database created by an earlier build (no mark) the state records:
        # `up` adopts and marks it, so a re-run keeps working.
        _sql(pg, f"COMMENT ON DATABASE {name} IS NULL")
        assert commands.up(_ctx(root, environ), commands.UpOptions()) == 0
        assert services.database_mark(pg, name) == commands.dev_mark(row)

        # Our marked database resets (and keeps its mark).
        _sql(pg, "CREATE TABLE ours (id int)", name)
        assert commands.reset(_ctx(root, environ), force=True) == 0
        assert not services.has_tables(pg, name)
        assert services.database_mark(pg, name) == commands.dev_mark(row)

        # Without the mark, reset refuses and drops nothing.
        _sql(pg, "CREATE TABLE ours (id int)", name)
        _sql(pg, f"COMMENT ON DATABASE {name} IS NULL")
        with pytest.raises(commands.DevError, match="did not create it"):
            commands.reset(_ctx(root, environ), force=True)
        assert services.has_tables(pg, name)

        # prune of a removed checkout keeps an unmarked database.
        services.ensure_database(pg, foreign)
        ctx = _ctx(root, environ)
        state = load_state(ctx.state_dir)
        assert state is not None
        gone = str(tmp_path / "gone")
        state.checkouts[gone] = CheckoutRow(
            path=gone,
            database=foreign,
            cache_index=40,
            broker_index=41,
            test_database="sandloom_test_gone",
        )
        save_state(ctx.state_dir, state)
        assert commands.prune(ctx) == 0
        assert services.database_exists(pg, foreign)
        after = load_state(ctx.state_dir)
        assert after is not None and gone not in after.checkouts
    finally:
        services.drop_database(pg, name)
        services.drop_database(pg, foreign)


def test_a_checkout_takes_only_the_indexes_the_stack_reports_empty(
    tmp_path: Path, environ: dict[str, str], fake_stack: FakeStack
) -> None:
    """Your own server may keep data in any index, and `dev reset` / `prune`
    flush ours: the stack says which indexes hold no key, and `dev up` gives
    the checkout two of those, never one that is in use."""
    fake_stack.keys = {index: {"theirs": "1"} for index in range(0, 40)}
    root = _app(tmp_path, "shop")
    ctx = _ctx(root, environ)
    state = commands.reconcile(
        ctx,
        None,
        commands.UpOptions(
            services="external",
            database_url="postgresql://me@127.0.0.1:5432/postgres",
            kv_url="redis://127.0.0.1:6379",
        ),
    )
    state.services = "external"
    state.external = {
        "database_url": "postgresql://me@127.0.0.1:5432/postgres",
        "kv_url": "redis://127.0.0.1:6379",
    }
    indexes = commands._external_indexes(ctx, state, str(root))
    assert min(indexes) >= 40
    assert not any(fake_stack.keys.get(index) for index in indexes)
    # No two empty indexes left: refused, naming the server, never reused.
    fake_stack.keys = {
        index: {"theirs": "1"} for index in range(fake_stack.index_count)
    }
    with pytest.raises(commands.DevError, match="no two empty database indexes"):
        commands._external_indexes(ctx, state, str(root))


def test_a_keyed_url_the_stack_refuses_is_refused_without_its_password() -> None:
    problem = commands.check_external(
        "postgresql://me@127.0.0.1:5432/postgres", "rediss://:secret@cache.local:6380"
    )
    assert problem is not None and "TLS" in problem
    assert "secret" not in problem
    other = commands.check_external(
        "postgresql://me@127.0.0.1:5432/postgres", "mysql://:secret@cache.local:3306"
    )
    assert other is not None and "secret" not in other


def test_a_postgres_url_whose_sslmode_would_be_dropped_is_refused() -> None:
    url = "postgresql://me:secret@db.local:5432/postgres?sslmode=require"
    with pytest.raises(ValueError, match="sslmode") as refused:
        services.Postgres.from_url(url)
    assert "secret" not in str(refused.value)


def test_the_cli_flags_switch_to_external(
    tmp_path: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[commands.UpOptions] = []
    monkeypatch.setattr(commands, "up", lambda ctx, options: seen.append(options) or 0)
    monkeypatch.chdir(_app(tmp_path, "shop"))
    argv = [
        "up",
        "--services",
        "external",
        "--database-url",
        "postgresql://me@127.0.0.1:5432/postgres",
        "--kv-url",
        "redis://127.0.0.1:6379",
    ]
    assert dev_cli.main(argv, environ=environ) == 0
    (options,) = seen
    assert options.services == "external"
    assert options.database_url == "postgresql://me@127.0.0.1:5432/postgres"


def test_dev_run_with_external_servers_down_says_what_to_do(
    tmp_path: Path,
    environ: dict[str, str],
    no_docker: list[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_stack: FakeStack,
) -> None:
    root = _app(tmp_path, "shop")
    ctx = _ctx(root, environ)
    monkeypatch.setattr(commands._services, "pg_ready", lambda pg: None)
    monkeypatch.setattr(commands._services, "ensure_database", lambda pg, n: False)
    monkeypatch.setattr(commands._services, "has_tables", lambda pg, n: True)
    monkeypatch.setattr(commands, "_claim_external", lambda *a, **k: None)
    options = commands.UpOptions(
        services="external",
        database_url="postgresql://me@127.0.0.1:5432/postgres",
        kv_url="redis://127.0.0.1:6379",
    )
    assert commands.up(ctx, options) == 0
    monkeypatch.setattr(commands._services, "pg_ready", lambda pg: "connection refused")
    with pytest.raises(commands.DevError) as refused:
        commands.run(_ctx(root, environ), port="auto", reload=False)
    message = str(refused.value)
    assert "\n" not in message
    assert "127.0.0.1:5432" in message and "brew services start" in message
    assert "--services docker" in message
    assert no_docker == []
    # The stack's own server down: the same one line, with how it starts.
    monkeypatch.setattr(commands._services, "pg_ready", lambda pg: None)
    fake_stack.down = "connection refused"
    with pytest.raises(commands.DevError) as refused_keyed:
        commands.run(_ctx(root, environ), port="auto", reload=False)
    keyed = str(refused_keyed.value)
    assert "\n" not in keyed
    assert f"127.0.0.1:{fake_stack.local_port}" in keyed
    assert fake_stack.start_command in keyed
    assert "--services docker" in keyed


def test_a_state_from_before_kv_url_says_to_run_up_again(
    tmp_path: Path, environ: dict[str, str]
) -> None:
    """A ``dev.json`` that holds ``redis_url`` (the key before it was renamed)
    names no keyed server: one line that says what to run, not a KeyError."""
    ctx = _ctx(_app(tmp_path, "shop"), environ)
    state = commands.reconcile(ctx, None, commands.UpOptions())
    state.services = "external"
    state.external = {
        "database_url": "postgresql://me@127.0.0.1:5432/postgres",
        "redis_url": "redis://127.0.0.1:6379",
    }
    with pytest.raises(commands.DevError) as refused:
        ctx.keyed(state)
    message = str(refused.value)
    assert "\n" not in message
    assert "--kv-url" in message and "dev up --services external" in message


def _state_from_before_kv_url(
    tmp_path: Path,
    environ: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    fake_stack: FakeStack,
) -> tuple[Path, dict[str, Any], tuple[int, ...]]:
    """What the CLI of before ``--kv-url`` left: an external ``dev.json`` that
    names the person's server as ``redis_url`` (here the local port), a row on
    the indexes that CLI found empty on it, and data of the person's own on
    another server that answers on the default local port."""
    monkeypatch.setattr(commands._services, "pg_ready", lambda pg: None)
    monkeypatch.setattr(commands._services, "ensure_database", lambda pg, n: False)
    monkeypatch.setattr(commands._services, "has_tables", lambda pg, n: True)
    monkeypatch.setattr(commands, "_claim_external", lambda *a, **k: None)
    root = _app(tmp_path, "shop")
    ctx = _ctx(root, environ)
    database = "postgresql://me@127.0.0.1:5432/postgres"
    options = commands.UpOptions(
        services="external", database_url=database, kv_url="redis://127.0.0.1:6390"
    )
    assert commands.up(ctx, options) == 0
    state = load_state(ctx.state_dir)
    assert state is not None
    state.external = {"database_url": database, "redis_url": "redis://127.0.0.1:6390"}
    save_state(ctx.state_dir, state)
    # The person's Homebrew server on the default port holds data in the row's
    # two indexes, and answers.
    monkeypatch.setattr(commands._services, "tcp_open", lambda h, p, timeout=2.0: True)
    indexes = state.checkouts[str(root)].indexes
    fake_stack.keys = {index: {"theirs": "data"} for index in indexes}
    return root, dict(state.external), indexes


def test_a_plain_up_on_a_state_from_before_kv_url_stops_and_touches_nothing(
    tmp_path: Path,
    environ: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    fake_stack: FakeStack,
) -> None:
    """The row's indexes were checked empty on the server the old state named.
    Falling back to whatever answers on the default port would put them on a
    server nobody checked, and `dev reset` would then flush the person's data."""
    root, external, indexes = _state_from_before_kv_url(
        tmp_path, environ, monkeypatch, fake_stack
    )
    ctx = _ctx(root, environ)
    with pytest.raises(commands.DevError) as refused:
        commands.up(ctx, commands.UpOptions())
    message = str(refused.value)
    assert "\n" not in message
    assert "--kv-url" in message and "dev up --services external" in message
    state = load_state(ctx.state_dir)
    assert state is not None
    assert state.external == external  # no kv_url written
    assert state.checkouts[str(root)].indexes == indexes
    assert all(fake_stack.keys[index] == {"theirs": "data"} for index in indexes)
    # `status` and `run` stop with the same line (the upgrade note says so).
    with pytest.raises(commands.DevError, match="--kv-url"):
        commands.status(ctx, as_json=True)
    with pytest.raises(commands.DevError, match="--kv-url"):
        commands.run(ctx, port="auto", reload=False)
    # `--database-url` alone does not name the server either.
    with pytest.raises(commands.DevError, match="--kv-url"):
        commands.up(
            ctx, commands.UpOptions(database_url="postgresql://me@127.0.0.1:5432/x")
        )
    # Naming it again, as the message says, works.
    again = commands.UpOptions(kv_url="redis://127.0.0.1:6390")
    assert commands.up(_ctx(root, environ), again) == 0
    state = load_state(ctx.state_dir)
    assert state is not None and state.external["kv_url"] == "redis://127.0.0.1:6390"


def test_naming_another_keyed_server_rechecks_the_indexes_of_every_row(
    tmp_path: Path,
    environ: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    fake_stack: FakeStack,
) -> None:
    """The row's pair was checked empty on the server the state named. The
    replacement server holds keys there: the pair is replaced before the state
    is saved (`reset` and `prune` flush it); the same server keeps it."""
    root, _external, indexes = _state_from_before_kv_url(
        tmp_path, environ, monkeypatch, fake_stack
    )
    keys = {index: dict(held) for index, held in fake_stack.keys.items()}
    again = commands.UpOptions(kv_url="redis://127.0.0.1:6390")
    assert commands.up(_ctx(root, environ), again) == 0
    state = load_state(_ctx(root, environ).state_dir)
    assert state is not None
    moved = state.checkouts[str(root)].indexes
    assert not set(moved) & set(indexes)
    assert not any(fake_stack.keys.get(index) for index in moved)
    assert fake_stack.keys == keys  # nothing of theirs touched
    # The same server named again: the allocation stays.
    assert commands.up(_ctx(root, environ), again) == 0
    after = load_state(_ctx(root, environ).state_dir)
    assert after is not None and after.checkouts[str(root)].indexes == moved
    # Another server, indexes free there: the pair stays (nothing to move).
    fake_stack.keys = {}
    other = commands.UpOptions(kv_url="redis://127.0.0.1:6391")
    assert commands.up(_ctx(root, environ), other) == 0
    last = load_state(_ctx(root, environ).state_dir)
    assert last is not None and last.checkouts[str(root)].indexes == moved


def _external_up_with_rows(
    tmp_path: Path,
    environ: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    pairs: dict[str, tuple[int, int]],
) -> tuple[Path, dict[str, Path]]:
    """``dev up --services external`` on the server at :6390, then one more
    checkout per name in ``pairs`` (a directory with a managed ``.env`` block,
    a row on that pair, which was never checked on any server)."""
    monkeypatch.setattr(commands._services, "pg_ready", lambda pg: None)
    monkeypatch.setattr(commands._services, "ensure_database", lambda pg, n: False)
    monkeypatch.setattr(commands._services, "has_tables", lambda pg, n: True)
    monkeypatch.setattr(commands, "_claim_external", lambda *a, **k: None)
    root = _app(tmp_path, "shop")
    ctx = _ctx(root, environ)
    options = commands.UpOptions(
        services="external",
        database_url="postgresql://me@127.0.0.1:5432/postgres",
        kv_url="redis://127.0.0.1:6390",
    )
    assert commands.up(ctx, options) == 0
    state = load_state(ctx.state_dir)
    assert state is not None
    others: dict[str, Path] = {}
    for name, (cache, broker) in pairs.items():
        checkout = tmp_path / name
        checkout.mkdir()
        (checkout / ".env").write_text("JWT_SECRET=x\n" + envfile.empty_block("Redis"))
        state.checkouts[str(checkout)] = CheckoutRow(
            path=str(checkout),
            database=f"shop_{name}",
            cache_index=cache,
            broker_index=broker,
            test_database=f"shop_{name}_test",
        )
        others[name] = checkout
    save_state(ctx.state_dir, state)
    return root, others


def test_going_external_docker_external_rechecks_the_rows_of_the_docker_period(
    tmp_path: Path,
    environ: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    fake_stack: FakeStack,
) -> None:
    """A switch to Docker keeps the recorded server, and a checkout that runs
    `dev up` meanwhile gets indexes nobody checked on the person's server.
    Naming no `--kv-url` on the way back reuses that record: the rows are
    still checked again (the rows were not checked on it), so `reset` and
    `prune` never flush the person's own keys."""
    root, others = _external_up_with_rows(
        tmp_path, environ, monkeypatch, {"wt": (3, 4)}
    )
    ctx = _ctx(root, environ)
    state = load_state(ctx.state_dir)
    assert state is not None
    state.services = "docker"  # `dev up --services docker`: `external` is kept
    save_state(ctx.state_dir, state)
    fake_stack.keys = {3: {"theirs": "a"}, 4: {"theirs": "b"}}  # on their server
    assert commands.up(_ctx(root, environ), commands.UpOptions(services="external")) == 0
    after = load_state(ctx.state_dir)
    assert after is not None
    row = after.checkouts[str(others["wt"])]
    assert not set(row.indexes) & {3, 4}
    assert (
        f"/{row.cache_index}" in (others["wt"] / ".env").read_text(encoding="utf-8")
    )
    # The worktree is gone and `prune` flushes the pair of its row.
    monkeypatch.setattr(
        commands._services, "database_mark", lambda pg, name: commands.dev_mark(row)
    )
    monkeypatch.setattr(commands._services, "drop_database", lambda pg, name: None)
    monkeypatch.setattr(commands, "_prune_test_databases", lambda c, s: [])
    shutil.rmtree(others["wt"])
    assert commands.prune(_ctx(root, environ)) == 0
    assert fake_stack.keys[3] == {"theirs": "a"}
    assert fake_stack.keys[4] == {"theirs": "b"}


def test_a_changed_keyed_server_rewrites_the_blocks_of_every_other_checkout(
    tmp_path: Path,
    environ: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    fake_stack: FakeStack,
) -> None:
    """A row whose pair is empty on the new server keeps its indexes but its
    `.env` block still names the old server: every other checkout is written."""
    root, others = _external_up_with_rows(
        tmp_path, environ, monkeypatch, {"free": (3, 4), "held": (5, 6)}
    )
    fake_stack.keys = {5: {"theirs": "a"}}  # only `held` meets keys there
    ctx = _ctx(root, environ)
    again = commands.UpOptions(kv_url="redis://127.0.0.1:6391")
    assert commands.up(ctx, again) == 0
    state = load_state(ctx.state_dir)
    assert state is not None
    assert state.checkouts[str(others["free"])].indexes == (3, 4)
    assert state.checkouts[str(others["held"])].indexes != (5, 6)
    for name in others:
        assert "127.0.0.1:6391" in (others[name] / ".env").read_text(encoding="utf-8")
    warning = ctx.err.getvalue()  # type: ignore[attr-defined]
    assert "1 checkout(s) held keys" in warning and "restart" in warning
    assert commands.REWRITE_PENDING not in state.external


def test_a_rewrite_that_failed_is_repeated_by_the_next_up_on_the_same_server(
    tmp_path: Path,
    environ: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    fake_stack: FakeStack,
) -> None:
    """The state names the new server before any other checkout is written;
    one that could not be written is named, kept pending, and written by the
    next `dev up`, which finds the recorded server unchanged."""
    root, others = _external_up_with_rows(
        tmp_path, environ, monkeypatch, {"first": (3, 4), "broken": (5, 6)}
    )
    real = commands.write_checkout_files

    def write(ctx: commands.Context, state: Any, row: CheckoutRow) -> list[str]:
        if row.path == str(others["broken"]):
            raise commands.DevError(f"{row.path}/.env: bad INFRA")
        return real(ctx, state, row)

    monkeypatch.setattr(commands, "write_checkout_files", write)
    ctx = _ctx(root, environ)
    again = commands.UpOptions(kv_url="redis://127.0.0.1:6391")
    assert commands.up(ctx, again) == 0
    saved = load_state(ctx.state_dir)
    assert saved is not None
    assert saved.external["kv_url"] == "redis://127.0.0.1:6391"
    assert saved.external[commands.REWRITE_PENDING]
    assert "bad INFRA" in ctx.err.getvalue()  # type: ignore[attr-defined]
    assert "6391" in (others["first"] / ".env").read_text(encoding="utf-8")
    assert "6391" not in (others["broken"] / ".env").read_text(encoding="utf-8")
    monkeypatch.setattr(commands, "write_checkout_files", real)
    assert commands.up(_ctx(root, environ), commands.UpOptions()) == 0
    assert "6391" in (others["broken"] / ".env").read_text(encoding="utf-8")
    done = load_state(ctx.state_dir)
    assert done is not None and commands.REWRITE_PENDING not in done.external


def test_reset_and_prune_name_the_keyed_server_before_dropping_a_database(
    tmp_path: Path,
    environ: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    fake_stack: FakeStack,
) -> None:
    """A state that names no keyed server stops `dev reset` and `dev prune`
    before the database is dropped: never a fresh database next to the stale
    keys, never a row left whose database is gone."""
    root, _external, _indexes = _state_from_before_kv_url(
        tmp_path, environ, monkeypatch, fake_stack
    )
    dropped: list[str] = []
    monkeypatch.setattr(
        commands._services, "drop_database", lambda pg, name: dropped.append(name)
    )
    monkeypatch.setattr(commands._services, "sessions", lambda *a, **k: {})
    monkeypatch.setattr(commands._services, "mark_database", lambda *a: None)
    monkeypatch.setattr(commands, "_prune_test_databases", lambda ctx, state: [])
    ctx = _ctx(root, environ)
    state = load_state(ctx.state_dir)
    assert state is not None
    row = state.checkouts[str(root)]
    gone = str(tmp_path / "gone")
    gone_row = CheckoutRow(
        path=gone,
        database="shop_gone",
        cache_index=40,
        broker_index=41,
        test_database="sandloom_test_gone",
    )
    marks = {
        row.database: commands.dev_mark(row),
        "shop_gone": commands.dev_mark(gone_row),
    }
    monkeypatch.setattr(
        commands._services, "database_mark", lambda pg, name: marks[name]
    )
    with pytest.raises(commands.DevError, match="--kv-url"):
        commands.reset(ctx, force=True)
    assert dropped == []
    # A removed checkout's row, marked as ours: prune stops before its
    # database is dropped.
    state.checkouts[gone] = gone_row
    save_state(ctx.state_dir, state)
    with pytest.raises(commands.DevError, match="--kv-url"):
        commands.prune(ctx)
    assert dropped == []
    after = load_state(ctx.state_dir)
    assert after is not None and gone in after.checkouts
    assert all(fake_stack.keys[index] == {"theirs": "data"} for index in row.indexes)


def test_prune_touches_nothing_when_no_keyed_server_is_named(
    tmp_path: Path,
    environ: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    fake_stack: FakeStack,
) -> None:
    """Before either pass: the pass over test databases drops stale marked
    `sandloom_test_` databases on the person's PostgreSQL without a row, so the
    refusal comes first, with no database, key or registry entry touched."""
    root, _external, _indexes = _state_from_before_kv_url(
        tmp_path, environ, monkeypatch, fake_stack
    )
    touched: list[str] = []
    monkeypatch.setattr(
        commands._services, "drop_database", lambda pg, name: touched.append(name)
    )
    monkeypatch.setattr(
        commands,
        "_prune_test_databases",
        lambda ctx, state: touched.append("test databases") or [],
    )
    ctx = _ctx(root, environ)
    stale = {5999: str(tmp_path / "gone")}
    commands.save_registry(ctx.home, stale)
    keys = {index: dict(held) for index, held in fake_stack.keys.items()}
    with pytest.raises(commands.DevError, match="--kv-url"):
        commands.prune(ctx)
    assert touched == []
    assert commands.load_registry(ctx.home) == stale
    assert fake_stack.keys == keys


def test_without_a_package_for_the_stack_the_command_names_what_to_install(
    tmp_path: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(driver: str) -> Any:
        raise stack_service.StackServiceError(f"no package for {driver}: install it")

    ctx = _ctx(_app(tmp_path, "shop"), environ)
    monkeypatch.setattr(stack_service, "_discover", missing)
    with pytest.raises(commands.DevError, match="install it"):
        commands.up(ctx, commands.UpOptions(services="external", kv_url="redis://h"))


def test_a_postgres_url_round_trips() -> None:
    pg = services.Postgres.from_url("postgresql+asyncpg://me:p%40ss@db.local:6543/x")
    assert (pg.host, pg.port, pg.user, pg.password) == ("db.local", 6543, "me", "p@ss")
    assert pg.url("shop") == "postgresql+asyncpg://me:p%40ss@db.local:6543/shop"
    bare = services.Postgres.from_url("postgresql://me@127.0.0.1/postgres")
    assert bare.port == 5432 and bare.url("shop").endswith("://me@127.0.0.1:5432/shop")


# ----------------------------------------------------------------- banner


class _App(BaseHTTPRequestHandler):
    routes = {"/health/simple": 200, "/admin/": 200, "/docs": 404}

    def do_GET(self) -> None:
        self.send_response(self.routes.get(self.path, 404))
        self.end_headers()

    def log_message(self, *_args: Any) -> None:
        pass


def test_the_banner_lists_what_is_running_once_the_app_answers() -> None:
    server = HTTPServer(("127.0.0.1", 0), _App)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    out = io.StringIO()
    try:
        stop = threading.Event()
        shown = commands.announce_when_up(
            out, ui.PLAIN, "shop", url, stop, interval=0.05
        )
    finally:
        server.shutdown()
    assert shown
    text = out.getvalue()
    assert "shop is running" in text
    rows = {
        line.split()[0]: line.split()[-1] for line in text.splitlines() if url in line
    }
    assert rows == {
        "App": f"{url}/",
        "Admin": f"{url}/admin/",
        "Health": f"{url}/health",
    }
    assert "API docs" not in text  # /docs answered 404
    assert text.count("is running") == 1


def test_no_banner_when_the_server_stops_first() -> None:
    stop = threading.Event()
    stop.set()
    out = io.StringIO()
    assert not commands.announce_when_up(
        out, ui.PLAIN, "shop", "http://127.0.0.1:9", stop, interval=0.01
    )
    assert out.getvalue() == ""


# ------------------------------------------------- two apps in one repository


def test_two_apps_in_one_repository_share_containers_not_state(
    tmp_path: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A monorepo with two apps: one compose project (one container set),
    but each app its own checkout row, database, test database, app port and
    `.env` block."""
    repo = tmp_path / "monorepo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    apps = []
    for name in ("shop", "desk"):
        root = repo / "apps" / name
        (root / "deploy").mkdir(parents=True)
        (root / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n')
        (root / ".env").write_text("JWT_SECRET=x\n" + envfile.empty_block("Redis"))
        apps.append(root)
    shop, desk = (_ctx(root, environ) for root in apps)
    assert shop.identity.repository == desk.identity.repository
    assert shop.state_dir == desk.state_dir
    assert shop.root != desk.root
    assert shop.identity.database != desk.identity.database
    assert shop.identity.test_database != desk.identity.test_database
    # One state: the second app reconciles to the first app's compose project.
    state = commands.reconcile(shop, None, commands.UpOptions())
    state.ports = {
        "db": 24101,
        "redis": 24102,
        "postgres_test": 24103,
        "redis_test": 24104,
    }
    again = commands.reconcile(desk, state, commands.UpOptions())
    assert again.project == state.project
    rows = [commands.checkout_row(ctx, state) for ctx in (shop, desk)]
    assert rows[0].database != rows[1].database
    assert rows[0].indexes != rows[1].indexes
    assert rows[0].test_database != rows[1].test_database
    free = {8000: False, 8001: True, 8002: True}
    ports = [
        commands.ensure_app_port(ctx, state, is_free=lambda p: free.get(p, True))
        for ctx in (shop, desk)
    ]
    assert ports[0] != ports[1]
    for ctx, row in zip((shop, desk), rows, strict=True):
        commands.write_checkout_files(ctx, state, row)
    blocks = [envfile.read_block((root / ".env").read_text()) for root in apps]
    assert blocks[0]["DATABASE_URL"] != blocks[1]["DATABASE_URL"]
    caches = [json.loads(b["INFRA"])["cache"]["default"]["url"] for b in blocks]
    assert caches[0] != caches[1]


def test_compose_files_that_differ_only_in_the_app_name_are_the_same() -> None:
    shop = "services:\n  db:\n    environment:\n      POSTGRES_DB: shop\n"
    desk = "services:\n  db:\n    environment:\n      POSTGRES_DB: desk\n"
    assert commands._same_services(shop, desk)
    assert not commands._same_services(shop, shop.replace("db:", "pg:"))
