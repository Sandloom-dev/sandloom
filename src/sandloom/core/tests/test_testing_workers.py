"""pytest-xdist isolation helpers (sandloom.core.testing.workers).

The database half, with no harness family loaded: what a worker gets when no
package contributes a backend. The Redis indexes of a worker are the
``sandloom-redis`` family's and are tested in
``packages/sandloom-redis/tests/test_testing_workers.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from sandloom.core.infra.worker_isolation import worker_redis_url
from sandloom.core.testing import families, workers
from sandloom.core.tests._root_support import redis_free_infra_env

_REAL_ENSURE_DATABASE = workers.ensure_database
BASE = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5433/sandloom_test",
    "CACHE_URL": "redis://localhost:6380/15",
    "BROKER_URL": "redis://localhost:6380/14",
}


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    created: list[str] = []
    # reload_test_env() writes os.environ directly: restore it afterwards, or
    # a fake DATABASE_URL leaks into every later test of this process.
    saved = dict(os.environ)
    for var in workers.OVERRIDABLE_VARS:  # the run's own overrides stay out
        monkeypatch.delenv(workers.OVERRIDE_PREFIX + var, raising=False)
    # No harness family: the worker rewrite is the database's alone, whatever
    # packages are installed (a Redis family would also rewrite the fake indexes).
    monkeypatch.setattr(families, "_loaded", ())
    monkeypatch.setattr(workers, "_applied", {})
    monkeypatch.setattr(workers, "ensure_database", created.append)
    yield created
    os.environ.clear()
    os.environ.update(saved)


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
    # no family contributes a backend: the Redis URLs are the base ones
    assert env["CACHE_URL"] == BASE["CACHE_URL"]
    assert env["BROKER_URL"] == BASE["BROKER_URL"]
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
    assert worker_redis_url("redis://:pw@h:1", 0, 16) == "redis://:pw@h:1/16"
    assert worker_redis_url("rediss://h:1/3?x=1", 1, 16) == ("rediss://h:1/35?x=1")


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
    assert values["BROKER_URL"] == os.environ["BROKER_URL"]


def test_explicit_overrides_win_over_env_test_on_every_load_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SANDLOOM_TEST_<VAR> beats the file; a bare shell DATABASE_URL still does not."""
    from sandloom.core.testing.fixtures import load_test_env

    env_file = tmp_path / ".env.test"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in BASE.items()))
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    own_db = "postgresql+asyncpg://u:p@localhost:5433/sandloom_test_mine"
    own_cache = "redis://localhost:6380/11"
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://dev/dev_db")
    monkeypatch.setenv("CACHE_URL", "redis://dev/0")
    monkeypatch.setenv("BROKER_URL", "redis://dev/1")
    monkeypatch.setenv("SANDLOOM_TEST_DATABASE_URL", own_db)
    monkeypatch.setenv("SANDLOOM_TEST_CACHE_URL", own_cache)

    load_test_env([env_file])
    assert os.environ["DATABASE_URL"] == own_db
    assert os.environ["CACHE_URL"] == own_cache
    # No override for BROKER_URL: the file wins over the stray shell value.
    assert os.environ["BROKER_URL"] == BASE["BROKER_URL"]

    os.environ["DATABASE_URL"] = "postgresql+asyncpg://dev/dev_db"
    workers.reload_test_env(env_file)
    assert os.environ["DATABASE_URL"] == own_db

    values = workers.worker_env_values(env_file)
    assert values["DATABASE_URL"] == own_db
    assert values["CACHE_URL"] == own_cache
    assert values["BROKER_URL"] == BASE["BROKER_URL"]


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


#: ``INFRA`` as a flat-free ``.env.test`` declares it: every Redis instance has
#: its own url, and an instance on another driver keeps what it has.
INFRA_FILE = {
    "cache": {"default": {"driver": "redis", "url": "redis://localhost:6380/15"}},
    "lease": {"beat": {"driver": "redis", "url": "redis://localhost:6380/15"}},
    "taskqueue": {
        "default": {"driver": "celery", "broker": "redis://localhost:6380/14"}
    },
    "storage": {"default": {"driver": "local", "url": "file:///tmp/x"}},
}
MINE_CACHE = "redis://localhost:6380/11"
MINE_BROKER = "redis://localhost:6380/12"


def _fake_seams(
    monkeypatch: pytest.MonkeyPatch, *, driver: str | None, fills: dict[str, str]
) -> None:
    """The composition's default driver and what that driver takes from a flat
    setting, as the seams answer them for an installed driver package."""
    from sandloom.core.testing import family_seams

    monkeypatch.setattr(
        family_seams, "default_driver", lambda capability, name, root=None: driver
    )
    monkeypatch.setattr(
        family_seams, "flat_url_options", lambda capability, name, d: fills
    )


def test_the_override_is_merged_into_infra_with_no_flat_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    _fake_seams(monkeypatch, driver=None, fills={"url": "cache_url"})
    env_file = tmp_path / ".env.test"
    env_file.write_text(
        f"DATABASE_URL={BASE['DATABASE_URL']}\nINFRA={json.dumps(INFRA_FILE)}\n"
    )
    for var in ("CACHE_URL", "BROKER_URL", "PYTEST_XDIST_WORKER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SANDLOOM_TEST_CACHE_URL", MINE_CACHE)
    monkeypatch.setenv("SANDLOOM_TEST_BROKER_URL", MINE_BROKER)

    workers.reload_test_env(env_file)
    infra = json.loads(os.environ["INFRA"])
    assert infra["cache"]["default"]["url"] == MINE_CACHE
    assert infra["lease"]["beat"]["url"] == MINE_CACHE  # explicit: nothing follows
    assert infra["taskqueue"]["default"]["broker"] == MINE_BROKER
    assert infra["storage"]["default"]["url"] == "file:///tmp/x"
    # No flat variable in the file: none is invented.
    assert "CACHE_URL" not in os.environ
    assert "BROKER_URL" not in os.environ
    assert workers.worker_env_values(env_file)["INFRA"] == os.environ["INFRA"]


def test_the_override_still_reaches_the_flat_variables_a_file_sets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    _fake_seams(monkeypatch, driver=None, fills={"url": "cache_url"})
    env_file = tmp_path / ".env.test"
    env_file.write_text(
        "".join(f"{k}={v}\n" for k, v in BASE.items())
        + f"INFRA={json.dumps(INFRA_FILE)}\n"
    )
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setenv("SANDLOOM_TEST_CACHE_URL", MINE_CACHE)
    # no BROKER override: its flat variable and INFRA entry stay the file's

    workers.reload_test_env(env_file)
    assert os.environ["CACHE_URL"] == MINE_CACHE
    assert os.environ["BROKER_URL"] == BASE["BROKER_URL"]
    infra = json.loads(os.environ["INFRA"])
    assert infra["cache"]["default"]["url"] == MINE_CACHE
    assert infra["taskqueue"]["default"]["broker"] == "redis://localhost:6380/14"


def test_the_default_cache_is_created_only_for_a_driver_that_takes_the_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    overrides = {"CACHE_URL": MINE_CACHE}
    _fake_seams(monkeypatch, driver="redis", fills={"url": "cache_url"})
    created = json.loads(workers.infra_with_overrides(None, overrides) or "")
    assert created == {"cache": {"default": {"driver": "redis", "url": MINE_CACHE}}}
    # a driver of the composition that fills no url (memory) is left alone ...
    _fake_seams(monkeypatch, driver="memory", fills={})
    assert workers.infra_with_overrides(None, overrides) == "{}"
    # ... and so is a composition that names no driver at all
    _fake_seams(monkeypatch, driver=None, fills={"url": "cache_url"})
    assert workers.infra_with_overrides(None, overrides) == "{}"


def test_a_declared_driver_beats_the_composition_when_the_default_is_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    _fake_seams(monkeypatch, driver="memory", fills={"url": "cache_url"})
    infra = json.dumps({"cache": {"default": {"driver": "redis"}}})
    merged = json.loads(
        workers.infra_with_overrides(infra, {"CACHE_URL": MINE_CACHE}) or ""
    )
    assert merged["cache"]["default"] == {"driver": "redis", "url": MINE_CACHE}


def test_a_malformed_infra_or_no_override_leaves_infra_as_it_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_seams(monkeypatch, driver="redis", fills={"url": "cache_url"})
    assert workers.infra_with_overrides("{not json", {"CACHE_URL": MINE_CACHE}) == (
        "{not json"
    )
    assert workers.infra_with_overrides("[1]", {"CACHE_URL": MINE_CACHE}) == "[1]"
    assert workers.infra_with_overrides('{"a": {}}', {}) == '{"a": {}}'
    assert workers.infra_with_overrides(None, {"CACHE_URL": ""}) is None
    # a cache that is not an object of instances is the settings' to refuse
    odd = '{"cache": []}'
    assert workers.infra_with_overrides(odd, {"CACHE_URL": MINE_CACHE}) == odd


def test_a_non_redis_url_of_an_instance_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    _fake_seams(monkeypatch, driver=None, fills={})
    infra = json.dumps(
        {
            "lease": {"beat": {"driver": "x", "url": "memory://"}},
            "taskqueue": {"q": {"driver": "celery", "broker": "amqp://h/"}},
        }
    )
    merged = workers.infra_with_overrides(
        infra, {"CACHE_URL": MINE_CACHE, "BROKER_URL": MINE_BROKER}
    )
    assert json.loads(merged or "") == json.loads(infra)


def test_only_instances_on_the_defaults_database_take_the_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Instances meant to live on separate databases keep apart: the override
    reaches the default cache and what sat on its server and database (a url
    without a path is database 0), nothing else."""
    import json

    _fake_seams(monkeypatch, driver=None, fills={"url": "cache_url"})
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


def test_the_flat_cache_url_names_the_default_when_infra_declares_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    _fake_seams(monkeypatch, driver="redis", fills={"url": "cache_url"})
    infra = json.dumps(
        {
            "lease": {
                "beat": {"driver": "redis", "url": "redis://localhost:6380/15"},
                "apart": {"driver": "redis", "url": "redis://localhost:6380/3"},
            }
        }
    )
    merged = json.loads(
        workers.infra_with_overrides(
            infra,
            {"CACHE_URL": MINE_CACHE},
            flat={"CACHE_URL": "redis://localhost:6380/15", "BROKER_URL": None},
        )
        or ""
    )
    assert merged["cache"]["default"]["url"] == MINE_CACHE  # created
    assert merged["lease"]["beat"]["url"] == MINE_CACHE
    assert merged["lease"]["apart"]["url"] == "redis://localhost:6380/3"
    # without the flat variable nothing names the default's database: only the
    # default itself is pointed
    alone = json.loads(
        workers.infra_with_overrides(infra, {"CACHE_URL": MINE_CACHE}) or ""
    )
    assert alone["lease"]["beat"]["url"] == "redis://localhost:6380/15"


def test_a_declared_backend_follows_a_replaced_broker_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    _fake_seams(monkeypatch, driver=None, fills={"broker": "broker_url"})
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

    _fake_seams(monkeypatch, driver=None, fills={"url": "cache_url"})
    env_file = tmp_path / ".env.test"
    env_file.write_text(
        f"DATABASE_URL={BASE['DATABASE_URL']}\nINFRA={json.dumps(INFRA_FILE)}\n"
    )
    monkeypatch.setenv("SANDLOOM_TEST_CACHE_URL", MINE_CACHE)
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
    workers.reload_test_env(env_file)
    assert os.environ["DATABASE_URL"].endswith("_gw1")
    assert json.loads(os.environ["INFRA"])["lease"]["beat"]["url"] == MINE_CACHE


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
