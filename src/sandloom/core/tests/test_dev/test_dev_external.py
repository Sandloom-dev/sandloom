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
        "ENVIRONMENT=dev\nJWT_SECRET=change_me\n" + envfile.empty_block()
    )
    (root / ".env").write_text("JWT_SECRET=x\n" + envfile.empty_block())
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
        (root / ".env").write_text("JWT_SECRET=x\n" + envfile.empty_block())
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
