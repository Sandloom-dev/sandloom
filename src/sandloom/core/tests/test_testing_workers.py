"""pytest-xdist isolation helpers (sandloom.core.testing.workers).

The database half, with a stand-in family that only owns URLs: what a worker gets
when no package contributes a backend, and how the ``SANDLOOM_TEST_*`` URL
overrides ask the families. The Redis indexes of a worker (and the layout of a
URL) are the ``sandloom-redis`` family's and are tested in
``packages/sandloom-redis/tests/test_testing_workers.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from sandloom.core.testing import families, session_checks, workers
from sandloom.core.tests._root_support import redis_free_infra_env
from sandloom.core.tests.test_testing_families import UrlFamily

_REAL_ENSURE_DATABASE = workers.ensure_database
BASE = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5433/sandloom_test",
    "STORAGE_PATH": "/tmp/sandloom-workers-test",
}


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    created: list[str] = []
    # reload_test_env() writes os.environ directly: restore it afterwards, or
    # a fake DATABASE_URL leaks into every later test of this process.
    saved = dict(os.environ)
    for var in workers.OVERRIDABLE_VARS:  # the run's own overrides stay out
        monkeypatch.delenv(workers.OVERRIDE_PREFIX + var, raising=False)
    # One stand-in family that owns URLs and rewrites nothing: the worker rewrite
    # is the database's alone, whatever packages are installed (a real family
    # would also rewrite the fake indexes).
    monkeypatch.setattr(families, "_loaded", (UrlFamily(),))
    monkeypatch.setattr(workers, "_applied", {})
    monkeypatch.setattr(workers, "ensure_database", created.append)
    yield created
    os.environ.clear()
    os.environ.update(saved)


def test_a_shell_cache_or_broker_url_is_not_a_run_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``CACHE_URL`` / ``BROKER_URL`` are no settings: a shell value of either
    must not reach the run's values (nothing reads them), while a shell
    ``DATABASE_URL`` still stands in for a missing one."""
    config = SimpleNamespace(
        rootpath=tmp_path, invocation_params=SimpleNamespace(args=())
    )
    monkeypatch.setenv("CACHE_URL", "redis://stray:6379/0")
    monkeypatch.setenv("BROKER_URL", "redis://stray:6379/1")
    monkeypatch.setenv("DATABASE_URL", BASE["DATABASE_URL"])
    values = session_checks.run_values(config)  # type: ignore[arg-type]
    assert values.get("DATABASE_URL") == BASE["DATABASE_URL"]
    assert "CACHE_URL" not in values and "BROKER_URL" not in values


def test_no_worker_means_no_rewrite() -> None:
    env = dict(BASE)
    assert workers.apply_worker_env(env) == {}
    assert env == BASE
    assert workers.worker_id({}) is None
    assert workers.worker_id({"PYTEST_XDIST_WORKER": "master"}) is None


def test_worker_gets_its_own_database_and_leaves_the_other_backends(
    _fresh_state: list[str],
) -> None:
    env = {**BASE, "PYTEST_XDIST_WORKER": "gw2"}
    workers.apply_worker_env(env)
    assert env["DATABASE_URL"].endswith("/sandloom_test_gw2")
    assert env["DATABASE_URL"].startswith("postgresql+asyncpg://u:p@localhost:5433/")
    # no family contributes a backend: everything else is the base one
    assert env["STORAGE_PATH"] == BASE["STORAGE_PATH"]
    assert _fresh_state == [env["DATABASE_URL"]]
    other = {**BASE, "PYTEST_XDIST_WORKER": "gw0"}
    assert workers.rewrite_for_worker(other, other).keys() == {"DATABASE_URL"}


def test_a_worker_asks_every_family_for_its_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rewrite of a family lands in the worker's environment beside the database's."""

    class Fake:
        name = "fake"

        def rewrite_worker(
            self, values: object, environ: object, worker_id: str, number: int
        ) -> dict[str, str]:
            return {"FAKE_URL": f"fake://{worker_id}/{number}"}

    monkeypatch.setattr(families, "_loaded", (Fake(),))
    env = {**BASE, "PYTEST_XDIST_WORKER": "gw3"}
    out = workers.rewrite_for_worker(env, env)
    assert out["FAKE_URL"] == "fake://gw3/3"
    assert out["DATABASE_URL"].endswith("/sandloom_test_gw3")


def test_apply_is_idempotent_and_survives_reloads() -> None:
    env = {**BASE, "PYTEST_XDIST_WORKER": "gw1"}
    workers.apply_worker_env(env)
    first = dict(env)
    assert workers.apply_worker_env(env) == {}  # already applied: untouched
    assert env == first
    env.update(BASE)  # a `.env.test` reload puts the base values back
    workers.apply_worker_env(env)
    assert env == first


def test_url_helpers_keep_everything_but_the_name_or_index() -> None:
    url = "postgresql+asyncpg://u:p%40ss@db:5432/app_test?ssl=disable"
    assert workers.worker_database_url(url, "gw3") == (
        "postgresql+asyncpg://u:p%40ss@db:5432/app_test_gw3?ssl=disable"
    )
    assert workers.worker_database_url(
        workers.worker_database_url(url, "gw3"), "gw3"
    ) == workers.worker_database_url(url, "gw3")


def test_reload_test_env_reapplies_the_worker_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env.test"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in BASE.items()))
    for key, value in BASE.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw4")
    workers.reload_test_env(env_file)
    assert os.environ["DATABASE_URL"].endswith("/sandloom_test_gw4")
    workers.reload_test_env(env_file)  # a second module re-loading the file
    assert os.environ["DATABASE_URL"].endswith("/sandloom_test_gw4")
    values = workers.worker_env_values(env_file)
    assert values["DATABASE_URL"] == os.environ["DATABASE_URL"]
    assert values["STORAGE_PATH"] == os.environ["STORAGE_PATH"]


#: ``INFRA`` as ``.env.test`` declares it: every Redis instance has its own url
#: (nothing follows another), and an instance on another driver keeps what it has.
INFRA_FILE = {
    "cache": {"default": {"driver": "redis", "url": "redis://localhost:6380/15"}},
    "lease": {"beat": {"driver": "redis", "url": "redis://localhost:6380/14"}},
    "taskqueue": {
        "default": {"driver": "celery", "broker": "redis://localhost:6380/14"}
    },
    "storage": {"default": {"driver": "local", "url": "file:///tmp/x"}},
}
MINE_CACHE = "redis://localhost:6380/11"
MINE_BROKER = "redis://localhost:6380/12"


def _write_env_test(path: Path, infra: object = INFRA_FILE) -> None:
    import json

    path.write_text(
        f"DATABASE_URL={BASE['DATABASE_URL']}\nINFRA='{json.dumps(infra)}'\n"
    )


def test_explicit_overrides_win_over_env_test_on_every_load_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SANDLOOM_TEST_<VAR> beats the file; a bare shell DATABASE_URL / INFRA
    still does not."""
    import json

    from sandloom.core.testing.fixtures import load_test_env

    env_file = tmp_path / ".env.test"
    _write_env_test(env_file)
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    own_db = "postgresql+asyncpg://u:p@localhost:5433/sandloom_test_mine"
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://dev/dev_db")
    monkeypatch.setenv("INFRA", '{"cache": {"default": {"url": "redis://dev/0"}}}')
    monkeypatch.setenv("SANDLOOM_TEST_DATABASE_URL", own_db)
    monkeypatch.setenv("SANDLOOM_TEST_CACHE_URL", MINE_CACHE)

    load_test_env([env_file])
    assert os.environ["DATABASE_URL"] == own_db
    infra = json.loads(os.environ["INFRA"])
    assert infra["cache"]["default"]["url"] == MINE_CACHE
    # No override for the broker: the file wins over the stray shell value.
    assert infra["taskqueue"]["default"]["broker"] == "redis://localhost:6380/14"
    assert infra["lease"]["beat"]["url"] == "redis://localhost:6380/14"

    os.environ["DATABASE_URL"] = "postgresql+asyncpg://dev/dev_db"
    workers.reload_test_env(env_file)
    assert os.environ["DATABASE_URL"] == own_db

    values = workers.worker_env_values(env_file)
    assert values["DATABASE_URL"] == own_db
    assert json.loads(values["INFRA"])["cache"]["default"]["url"] == MINE_CACHE


def test_the_override_is_merged_into_infra_and_sets_no_flat_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    env_file = tmp_path / ".env.test"
    _write_env_test(env_file)
    for var in ("CACHE_URL", "BROKER_URL", "PYTEST_XDIST_WORKER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SANDLOOM_TEST_CACHE_URL", MINE_CACHE)
    monkeypatch.setenv("SANDLOOM_TEST_BROKER_URL", MINE_BROKER)

    workers.reload_test_env(env_file)
    infra = json.loads(os.environ["INFRA"])
    assert infra["cache"]["default"]["url"] == MINE_CACHE
    assert infra["taskqueue"]["default"]["broker"] == MINE_BROKER
    # the lease sat on the broker's database: it follows the broker override
    assert infra["lease"]["beat"]["url"] == MINE_BROKER
    assert infra["storage"]["default"]["url"] == "file:///tmp/x"
    # No flat variable exists: none is invented.
    assert "CACHE_URL" not in os.environ
    assert "BROKER_URL" not in os.environ
    assert workers.worker_env_values(env_file)["INFRA"] == os.environ["INFRA"]
    out = workers.explicit_test_overrides(
        {"SANDLOOM_TEST_CACHE_URL": MINE_CACHE},
        current={"INFRA": json.dumps(INFRA_FILE)},
    )
    assert set(out) == {"INFRA"}


@pytest.mark.parametrize(
    ("infra", "var"),
    [
        (None, "CACHE_URL"),
        ("{}", "CACHE_URL"),
        ('{"cache": {"default": {"driver": "redis"}}}', "CACHE_URL"),
        ('{"cache": {"default": {"url": "memory://"}}}', "CACHE_URL"),
        ('{"cache": {"sessions": {"url": "redis://h:6379/0"}}}', "CACHE_URL"),
        ('{"cache": {"default": {"url": "redis://h:6379/0"}}}', "BROKER_URL"),
        ('{"taskqueue": {"default": {"driver": "celery"}}}', "BROKER_URL"),
    ],
)
def test_an_override_that_matches_no_declared_instance_refuses(
    infra: str | None, var: str
) -> None:
    """Never silent: a run that points ``SANDLOOM_TEST_*`` at its own Redis must
    not go on using the shared one because ``INFRA`` names nothing to replace."""
    with pytest.raises(workers.UnmatchedOverrideError, match=f"SANDLOOM_TEST_{var}"):
        workers.infra_with_overrides(infra, {var: MINE_CACHE})
    with pytest.raises(workers.UnmatchedOverrideError):
        workers.explicit_test_overrides(
            {f"SANDLOOM_TEST_{var}": MINE_CACHE},
            current={} if infra is None else {"INFRA": infra},
        )


def test_without_a_family_no_url_is_one_the_override_can_replace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kernel knows no URL form: a URL no installed family owns is no target,
    so the override refuses (never silently keeps the shared server)."""
    monkeypatch.setattr(families, "_loaded", ())
    infra = '{"cache": {"default": {"driver": "x", "url": "redis://h:6379/0"}}}'
    with pytest.raises(workers.UnmatchedOverrideError, match="installed backend"):
        workers.infra_with_overrides(infra, {"CACHE_URL": MINE_CACHE})
    assert workers.overrides_infra_can_take(infra) == ()


def test_the_family_decides_which_urls_sit_on_the_same_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Literal(UrlFamily):
        """Two URLs name one database only when they are the same string."""

        def same_database(self, stated: str, base: str) -> bool:
            return stated == base

    infra = json.dumps(
        {
            "cache": {"default": {"driver": "x", "url": "redis://h:6379/0"}},
            "lease": {"beat": {"driver": "x", "url": "redis://h:6379"}},
        }
    )
    together = json.loads(
        workers.infra_with_overrides(infra, {"CACHE_URL": MINE_CACHE}) or ""
    )
    assert together["lease"]["beat"]["url"] == MINE_CACHE
    monkeypatch.setattr(families, "_loaded", (Literal(),))
    apart = json.loads(
        workers.infra_with_overrides(infra, {"CACHE_URL": MINE_CACHE}) or ""
    )
    assert apart["cache"]["default"]["url"] == MINE_CACHE
    assert apart["lease"]["beat"]["url"] == "redis://h:6379"


def test_a_malformed_infra_or_no_override_leaves_infra_as_it_is() -> None:
    assert workers.infra_with_overrides("{not json", {"CACHE_URL": MINE_CACHE}) == (
        "{not json"
    )
    assert workers.infra_with_overrides("[1]", {"CACHE_URL": MINE_CACHE}) == "[1]"
    assert workers.infra_with_overrides('{"a": {}}', {}) == '{"a": {}}'
    assert workers.infra_with_overrides(None, {"CACHE_URL": ""}) is None
    assert workers.explicit_test_overrides({}, current={}) == {}


def test_a_non_redis_url_of_an_instance_is_left_alone() -> None:
    import json

    infra = json.dumps(
        {
            "cache": {
                "default": {"driver": "redis", "url": "redis://localhost:6380/1"}
            },
            "lease": {"beat": {"driver": "x", "url": "memory://"}},
            "taskqueue": {
                "default": {"driver": "celery", "broker": "redis://localhost:6380/2"},
                "q": {"driver": "celery", "broker": "amqp://h/"},
            },
        }
    )
    merged = workers.infra_with_overrides(
        infra, {"CACHE_URL": MINE_CACHE, "BROKER_URL": MINE_BROKER}
    )
    out = json.loads(merged or "")
    assert out["cache"]["default"]["url"] == MINE_CACHE
    assert out["taskqueue"]["default"]["broker"] == MINE_BROKER
    assert out["lease"] == {"beat": {"driver": "x", "url": "memory://"}}
    assert out["taskqueue"]["q"]["broker"] == "amqp://h/"


def test_only_instances_on_the_defaults_database_take_the_override() -> None:
    """Instances meant to live on separate databases keep apart: the override
    reaches the default and what sat on its server and database (a url without
    a path is database 0), nothing else."""
    import json

    infra = json.dumps(
        {
            "cache": {
                "default": {"driver": "redis", "url": "redis://localhost:6380/0"},
                "rate": {"driver": "redis", "url": "redis://other:6379/5"},
            },
            "lease": {
                "beat": {"driver": "redis", "url": "redis://localhost:6380/3"},
                "twin": {"driver": "redis", "url": "redis://localhost:6380"},
            },
        }
    )
    merged = json.loads(
        workers.infra_with_overrides(infra, {"CACHE_URL": "redis://h:6380/9"}) or ""
    )
    assert merged["cache"]["default"]["url"] == "redis://h:6380/9"
    assert merged["lease"]["twin"]["url"] == "redis://h:6380/9"
    assert merged["cache"]["rate"]["url"] == "redis://other:6379/5"
    assert merged["lease"]["beat"]["url"] == "redis://localhost:6380/3"


def test_a_declared_backend_follows_a_replaced_broker_only() -> None:
    import json

    infra = json.dumps(
        {
            "taskqueue": {
                "default": {
                    "driver": "celery",
                    "broker": "redis://localhost:6380/14",
                    "backend": "redis://localhost:6380/13",
                },
                "elsewhere": {
                    "driver": "celery",
                    "broker": "redis://other:6379/1",
                    "backend": "redis://other:6379/2",
                },
                "rpc": {
                    "driver": "celery",
                    "broker": "redis://localhost:6380/14",
                    "backend": "rpc://",
                },
            }
        }
    )
    merged = json.loads(
        workers.infra_with_overrides(infra, {"BROKER_URL": MINE_BROKER}) or ""
    )["taskqueue"]
    assert merged["default"]["broker"] == MINE_BROKER
    assert merged["default"]["backend"] == MINE_BROKER
    assert merged["rpc"] == {
        "driver": "celery",
        "broker": MINE_BROKER,
        "backend": "rpc://",
    }
    assert merged["elsewhere"] == {
        "driver": "celery",
        "broker": "redis://other:6379/1",
        "backend": "redis://other:6379/2",
    }


def test_the_overridden_infra_is_rewritten_per_xdist_worker_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker rewrite runs on the merged INFRA: with no Redis family loaded
    (this suite), INFRA is left as merged and only the database moves."""
    import json

    env_file = tmp_path / ".env.test"
    _write_env_test(env_file)
    monkeypatch.setenv("SANDLOOM_TEST_CACHE_URL", MINE_CACHE)
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
    workers.reload_test_env(env_file)
    assert os.environ["DATABASE_URL"].endswith("_gw1")
    infra = json.loads(os.environ["INFRA"])
    assert infra["cache"]["default"]["url"] == MINE_CACHE


def test_overrides_are_rewritten_per_xdist_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env.test"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in BASE.items()))
    monkeypatch.setenv(
        "SANDLOOM_TEST_DATABASE_URL", "postgresql+asyncpg://u:p@h:1/mine"
    )
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw2")
    workers.reload_test_env(env_file)
    assert os.environ["DATABASE_URL"] == "postgresql+asyncpg://u:p@h:1/mine_gw2"
    values = workers.worker_env_values(env_file)
    assert values["DATABASE_URL"] == "postgresql+asyncpg://u:p@h:1/mine_gw2"


def test_ensure_database_creates_the_worker_database() -> None:
    """Real PostgreSQL: the worker database exists afterwards, with citext."""
    from sandloom.core.testing.workers import worker_env_values

    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    url = os.environ.get("DATABASE_URL") or str(
        worker_env_values(env_test).get("DATABASE_URL") or ""
    )
    if not url.startswith("postgresql"):
        pytest.skip("PostgreSQL DATABASE_URL not configured")
    try:
        import psycopg2  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover
        pytest.skip("psycopg2 unavailable")
    target = workers.worker_database_url(url, "wtest")
    plain = url.replace("postgresql+asyncpg://", "postgresql://")
    try:
        _REAL_ENSURE_DATABASE(target)
    except psycopg2.OperationalError as exc:  # pragma: no cover
        pytest.skip(f"PostgreSQL unreachable: {exc}")
    name = target.rsplit("/", 1)[1].split("?", 1)[0]
    try:
        conn = psycopg2.connect(plain.rsplit("/", 1)[0] + f"/{name}")
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'citext'")
            assert cur.fetchone() == (1,)
        conn.close()
        _REAL_ENSURE_DATABASE(target)  # second call: no-op
    finally:
        admin = psycopg2.connect(plain.rsplit("/", 1)[0] + "/postgres")
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin.close()


def test_generate_opens_no_database_for_an_explicit_test_url() -> None:
    """A taken-port setup names its test database before the services are up."""
    example = Path(__file__).resolve().parents[4] / "examples/linkly"
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_XDIST")}
    env["PYTHONPATH"] = str(example)
    env["SANDLOOM_TEST_DATABASE_URL"] = (
        "postgresql+asyncpg://user:secret@127.0.0.1:1/nothing_listens_here"
    )
    env.update(
        redis_free_infra_env()
    )  # the example's Redis drivers: memory without them
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sandloom; sandloom.main()",
            "generate",
            "--check",
        ],
        cwd=example,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr


def test_generate_opens_no_database_on_a_worker_either() -> None:
    env = {**os.environ, "PYTEST_XDIST_WORKER": "gw0"}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(workers, "ensure_database", _refuse_a_connection)
        workers.apply_worker_env(dict(env), create_database=False)


def _refuse_a_connection(_url: str) -> None:
    raise AssertionError("no database may be opened")


# A url a person typed can hold a character that NFKC-normalises to ``#``: the
# stdlib ``urlsplit`` then raises a ValueError that quotes the netloc, password
# included. Every kernel place that parses such a url raises without it.
_SECRET = "S3CRETpw"
_HOSTILE = f"postgresql://user:{_SECRET}@host＃x:5432/db"


def _hostile_calls() -> dict[str, Callable[[], object]]:
    from sandloom.core import production_profile  # noqa: PLC0415 - cold: one test
    from sandloom.core.cli import wizard  # noqa: PLC0415 - cold: one test
    from sandloom.core.cli.dev import services  # noqa: PLC0415 - cold: one test
    from sandloom.core.infra.url_split import split_url  # noqa: PLC0415 - cold: one test
    from sandloom.core.testing import (  # noqa: PLC0415 - cold: one test
        connections,
        ownership,
        slots,
    )

    return {
        "split_url": lambda: split_url(_HOSTILE),
        "same_server": lambda: production_profile._same_server(_HOSTILE, _HOSTILE),
        "postgres.from_url": lambda: services.Postgres.from_url(_HOSTILE),
        "redis.from_url": lambda: services.RedisServer.from_url(_HOSTILE),
        "wizard._address": lambda: wizard._address(_HOSTILE),
        "worker_database_url": lambda: workers.worker_database_url(_HOSTILE, "gw0"),
        "sqlite_file": lambda: workers.sqlite_file(_HOSTILE),
        "slot_database_url": lambda: slots.slot_database_url(_HOSTILE, 2),
        "headroom": lambda: connections.server_headroom(_HOSTILE),
        "plain_dsn": lambda: ownership._plain_dsn(_HOSTILE),
    }


@pytest.mark.parametrize("name", sorted(_hostile_calls()))
def test_a_url_urlsplit_refuses_never_puts_its_password_in_the_error(
    name: str,
) -> None:
    with pytest.raises(ValueError, match="cannot be parsed") as caught:
        _hostile_calls()[name]()
    assert _SECRET not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_redact_does_not_echo_a_url_it_cannot_parse() -> None:
    from sandloom.core.cli.dev import services  # noqa: PLC0415 - cold: one test

    assert _SECRET not in services.redact(_HOSTILE)
    assert services.redact("redis://u:pw@h:6379/0") == "redis://u:<password>@h:6379/0"
