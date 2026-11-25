"""A deployment with ``ENVIRONMENT`` unset is not silently a development machine.

``settings.environment`` keeps reading ``dev`` when nothing set it (code that
gates on ``is_prod`` is unchanged), and ``settings.environment_explicit`` says
whether something did. Three things hang on it:

* a startup warning (the app, the worker and the scheduler boot through
  ``create_app``);
* the boot refusal SANDLOOM-B043: unset, with a database that is not on this
  machine;
* the verifier and the mint of ``sandloom dev token``
  (``test_dev_token_mint`` holds the token rows next to the claim-policy matrix).

Every row fails when the rule it names is removed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.app_factory import (
    _warn_startup_configuration,
    configuration_problems,
    enforce_configuration,
)
from sandloom.core.app_factory.config import (
    UNSET_ENVIRONMENT_WARNING,
    environment_unset_problem,
)
from sandloom.core.cli.doctor import WARN, Report, check_config
from sandloom.core.config import module_settings
from sandloom.core.config.common import ConfigurationError, Environment
from sandloom.core.config.settings import _LOAD_SNAPSHOT, Settings, settings
from sandloom.core.error_codes import CATALOGUE
from sandloom.core.sql_alchemy.url_hosts import (
    database_hosts,
    is_local_host,
    remote_database_target,
)

_STRONG = "9f3b7c1e5a2d4f6b8c0e1a3d5f7b9c2e4a6d8f0b1c3e5a7d9f2b4c6e8a0d1f3b"
_LOCAL_DB = "postgresql+asyncpg://dev:dev-pw@127.0.0.1:5433/shop"
_REMOTE_DB = "postgresql+asyncpg://app:s3cr3t-pw@db.internal.example:5432/shop"


@contextmanager
def process_environment(value: Environment | None) -> Iterator[None]:
    """The process ``settings`` as an app reads them with ``ENVIRONMENT=<value>``
    (``None``: the variable left unset, so the ``dev`` default and no mark that
    it was set), restored with its mark on exit. ``monkeypatch.setattr`` on a
    pydantic field marks it set for good, which would leak "explicit" into the
    tests that follow."""
    original = settings.environment
    was_set = settings.environment_explicit
    settings.__dict__["environment"] = Environment.dev if value is None else value
    if value is None:
        settings.model_fields_set.discard("environment")
    else:
        settings.model_fields_set.add("environment")
    try:
        yield
    finally:
        settings.__dict__["environment"] = original
        if was_set:
            settings.model_fields_set.add("environment")
        else:
            settings.model_fields_set.discard("environment")


@pytest.fixture(autouse=True)
def _no_environment_in_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """``Settings`` built below read the process environment too: a developer
    shell that exports ``ENVIRONMENT`` must not make a row's "unset" explicit."""
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("PGHOST", raising=False)


def _cfg(
    url: str = _LOCAL_DB, extra_infra: dict[str, Any] | None = None, **env: Any
) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        DATABASE_URL=url,
        INFRA={
            "cache": {"default": {"url": "redis://h:6379/0"}},
            "taskqueue": {"default": {"broker": "redis://h:6379/1"}},
            **(extra_infra or {}),
        },
        **env,
    )


# --- 1. which sources count as "set" -------------------------------------------
def test_nothing_set_reads_dev_and_is_not_explicit() -> None:
    cfg = _cfg()
    assert cfg.environment is Environment.dev  # the default value is unchanged
    assert cfg.environment_explicit is False
    assert cfg.is_dev


@pytest.mark.parametrize("value", ["dev", "test", "prod"])
def test_the_process_environment_counts(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENVIRONMENT", value)
    cfg = _cfg(JWT_SECRET=_STRONG)
    assert cfg.environment_explicit is True
    assert cfg.environment.value == value


def test_the_dotenv_file_counts(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("ENVIRONMENT=dev\n", encoding="utf-8")
    cfg = Settings(_env_file=str(dotenv), DATABASE_URL=_LOCAL_DB)  # type: ignore[call-arg]
    assert cfg.environment_explicit is True
    assert cfg.environment is Environment.dev


def test_a_dotenv_file_without_it_does_not_count(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("LOG_LEVEL=INFO\n", encoding="utf-8")
    cfg = Settings(_env_file=str(dotenv), DATABASE_URL=_LOCAL_DB)  # type: ignore[call-arg]
    assert cfg.environment_explicit is False


def _process_load(snapshot: module_settings.SourceSnapshot) -> Settings:
    """What the process load builds: ``Settings`` over one snapshot of the
    environment and ``.env`` (``settings._load_into``)."""
    token = _LOAD_SNAPSHOT.set(snapshot)
    try:
        return Settings(_env_file=None)  # type: ignore[call-arg]
    finally:
        _LOAD_SNAPSHOT.reset(token)


def test_the_process_load_sees_the_environment_variable(tmp_path: Path) -> None:
    snapshot = module_settings.SourceSnapshot(
        environ={
            "DATABASE_URL": _LOCAL_DB,
            "ENVIRONMENT": "test",
            "JWT_SECRET": _STRONG,
        },
        env_file=None,
    )
    cfg = _process_load(snapshot)
    assert cfg.environment_explicit is True
    assert cfg.environment is Environment.test


def test_the_process_load_sees_the_dotenv_file(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("ENVIRONMENT=dev\n", encoding="utf-8")
    snapshot = module_settings.SourceSnapshot(
        environ={"DATABASE_URL": _LOCAL_DB, "JWT_SECRET": _STRONG}, env_file=dotenv
    )
    cfg = _process_load(snapshot)
    assert cfg.environment_explicit is True


def test_the_process_load_without_either_is_not_explicit(tmp_path: Path) -> None:
    snapshot = module_settings.SourceSnapshot(
        environ={"DATABASE_URL": _LOCAL_DB, "JWT_SECRET": _STRONG},
        env_file=tmp_path / "missing.env",
    )
    assert _process_load(snapshot).environment_explicit is False


# --- the machine a database URL names ------------------------------------------
@pytest.mark.parametrize(
    "host",
    ["localhost", "LOCALHOST", "127.0.0.1", "127.8.9.10", "::1", "/var/run/postgresql"],
)
def test_this_machine(host: str) -> None:
    assert is_local_host(host)


@pytest.mark.parametrize(
    "host",
    ["db.internal.example", "10.0.0.5", "192.168.1.1", "2001:db8::1", "127.0.0.1.evil"],
)
def test_not_this_machine(host: str) -> None:
    assert not is_local_host(host)


def test_the_hosts_of_a_url_are_read_by_sqlalchemy() -> None:
    url = "postgresql+asyncpg://localhost:pa/ss@db.prod.example.com/x"
    assert database_hosts(url) == ["db.prod.example.com"]
    assert remote_database_target(url) == "host 'db.prod.example.com'"


# A url is local only when the allow-list shows it: whatever it does not let us
# read is remote (review of env-unset-not-dev, L1).
@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///./dev.db",
        "sqlite+aiosqlite:///:memory:",
        "postgresql+asyncpg://u:p@localhost/d",
        "postgresql+asyncpg://u:p@/d",  # libpq's default: the unix socket
        "postgresql+asyncpg://u:p@/d?host=/var/run/postgresql",
        "mysql+aiomysql://u:p@127.0.0.1:3306/d",
    ],
)
def test_the_target_of_a_local_url_is_none(url: str) -> None:
    assert remote_database_target(url) is None


@pytest.mark.parametrize(
    ("url", "phrase"),
    [
        (
            "postgresql+asyncpg://u:p@/db?dsn=postgresql://u:p@db.prod.example/app",
            "'dsn' query parameter",
        ),
        ("postgresql+asyncpg://u:p@localhost/db?DSN=x", "'dsn' query parameter"),
        ("postgresql+asyncpg://u:p@127.0.0.1/db?service=prod", "'service' query"),
        ("postgresql+psycopg://u:p@/db?service=prod", "'service' query"),
        ("mysql+aiomysql://u:p@/d", "a mysql url that names no host"),
        ("mssql+aioodbc://u:p@/d", "a mssql url that names no host"),
        ("foo://u:p@/d", "a foo url that names no host"),
        ("foo://u:p@db.example.com/d", "host 'db.example.com'"),
    ],
)
def test_the_target_of_a_url_that_is_not_shown_local(url: str, phrase: str) -> None:
    target = remote_database_target(url)
    assert target is not None and phrase in target
    assert "u:p" not in target and "db.prod.example" not in target


def test_a_dsn_parameter_wins_over_a_local_pghost() -> None:
    url = "postgresql+asyncpg://u:p@/db?dsn=postgresql://u:p@db.prod.example/app"
    assert remote_database_target(url, "127.0.0.1") is not None


# --- 4. SANDLOOM-B043: unset with a database that is not on this machine --------
@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://u:p@localhost:5432/d",
        "postgresql+asyncpg://u:p@127.0.0.1:5433/d",
        "postgresql+asyncpg://u:p@127.0.0.7/d",
        "postgresql+asyncpg://u:p@[::1]:5432/d",
        "postgresql+asyncpg://u:p@/d?host=/var/run/postgresql",
        "sqlite+aiosqlite:///./dev.db",
        "postgresql+asyncpg://u:p@/d",  # no host, no PGHOST: the default socket
    ],
)
def test_unset_with_a_database_on_this_machine_starts(url: str) -> None:
    cfg = _cfg(url)
    assert environment_unset_problem(cfg) is None
    assert not [p for p in configuration_problems(cfg) if "SANDLOOM-B043" in p]
    enforce_configuration(cfg)  # no refusal


def test_an_in_memory_sqlite_database_is_this_machine() -> None:
    # SANDLOOM-B032 refuses it for its own reason: B043 does not
    cfg = _cfg("sqlite+aiosqlite:///:memory:")
    assert environment_unset_problem(cfg) is None


@pytest.mark.parametrize(
    "url",
    [
        _REMOTE_DB,
        "postgresql+asyncpg://u:p@10.0.0.5:5432/d",
        "postgresql+asyncpg://u:p@[2001:db8::1]/d",
        "postgresql+asyncpg://u:p@localhost/d?host=db.example.com",
        "postgresql+asyncpg://u:p@127.0.0.1/d?hostaddr=10.1.1.1",
        # not shown local: the url names no host we can judge
        "postgresql+asyncpg://u:p@/db?dsn=postgresql://u:p@db.prod.example/app",
        "postgresql+asyncpg://u:p@localhost/db?service=prod",
        "mysql+aiomysql://u:p@/d",
    ],
)
def test_unset_with_a_remote_database_does_not_start(url: str) -> None:
    cfg = _cfg(url)
    problem = environment_unset_problem(cfg)
    assert problem is not None
    assert "SANDLOOM-B043" in problem
    assert "ENVIRONMENT is not set" in problem
    assert problem in configuration_problems(cfg)
    with pytest.raises(ConfigurationError, match="SANDLOOM-B043"):
        enforce_configuration(cfg)


def test_the_refusal_names_the_host_and_never_a_credential() -> None:
    problem = environment_unset_problem(_cfg(_REMOTE_DB))
    assert problem is not None
    assert "db.internal.example" in problem
    for secret in ("s3cr3t-pw", "app:", "postgresql"):
        assert secret not in problem


def test_a_url_shared_by_two_labels_is_named_once() -> None:
    # DATABASE_URL and the `default` instance resolve to one url: one finding
    cfg = _cfg(
        _REMOTE_DB, extra_infra={"database": {"default": {"driver": "sqlalchemy"}}}
    )
    problem = environment_unset_problem(cfg)
    assert problem is not None
    assert problem.count("db.internal.example") == 1
    assert "DATABASE_URL and default (host 'db.internal.example')" in problem


def test_two_different_urls_are_both_named() -> None:
    cfg = _cfg(
        _REMOTE_DB,
        extra_infra={
            "database": {
                "default": {"driver": "sqlalchemy"},
                "analytics": {
                    "driver": "sqlalchemy",
                    "url": "postgresql+asyncpg://u:p@db.other.example/x",
                },
            }
        },
    )
    problem = environment_unset_problem(cfg)
    assert problem is not None
    assert "db.internal.example" in problem and "db.other.example" in problem
    assert "analytics (host 'db.other.example')" in problem


def test_pghost_decides_for_a_url_without_a_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "postgresql+asyncpg://u:p@/d"
    monkeypatch.setenv("PGHOST", "127.0.0.1")
    assert environment_unset_problem(_cfg(url)) is None
    monkeypatch.setenv("PGHOST", "db.internal.example")
    problem = environment_unset_problem(_cfg(url))
    assert problem is not None and "db.internal.example" in problem


def test_an_infra_database_instance_is_checked_too() -> None:
    cfg = _cfg(
        _LOCAL_DB,
        extra_infra={
            "database": {"analytics": {"driver": "sqlalchemy", "url": _REMOTE_DB}}
        },
    )
    problem = environment_unset_problem(cfg)
    assert problem is not None
    assert "analytics" in problem
    assert "db.internal.example" in problem


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://u:p@h:notaport/d",
        "postgresql+asyncpg://u:p@localhost:5432,db.example.com:5432/d",
    ],
)
def test_an_unparsable_url_is_refused_not_passed(url: str) -> None:
    # a url SQLAlchemy cannot read is not shown to be local, so it is not accepted
    problem = environment_unset_problem(_cfg(url))
    assert problem is not None
    assert "SANDLOOM-B043" in problem
    assert "does not parse" in problem


@pytest.mark.parametrize("value", ["dev", "test"])
def test_set_to_dev_or_test_a_remote_database_starts(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENVIRONMENT", value)
    cfg = _cfg(_REMOTE_DB)
    assert environment_unset_problem(cfg) is None
    assert not [p for p in configuration_problems(cfg) if "SANDLOOM-B043" in p]


def test_set_to_prod_nothing_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "prod")
    cfg = _cfg(_REMOTE_DB, JWT_SECRET=_STRONG)
    assert environment_unset_problem(cfg) is None
    assert not [p for p in configuration_problems(cfg) if "SANDLOOM-B043" in p]


def test_the_refusal_is_one_of_the_boot_checks_of_every_entry_point() -> None:
    """``enforce_configuration`` (the app, the worker, ``sandloom migrate`` and
    the ``sandloom`` commands run it) is what raises, with the catalogue code."""
    assert any(entry.code == "SANDLOOM-B043" for entry in CATALOGUE)
    with pytest.raises(ConfigurationError, match="set ENVIRONMENT=prod"):
        enforce_configuration(_cfg(_REMOTE_DB))


# --- 3. the startup warning ------------------------------------------------------
def test_unset_logs_one_startup_warning(caplog: pytest.LogCaptureFixture) -> None:
    log = logging.getLogger("sandloom.app")
    with process_environment(None), caplog.at_level(logging.WARNING, "sandloom.app"):
        _warn_startup_configuration(log)
    warned = [r for r in caplog.records if "ENVIRONMENT is not set" in r.getMessage()]
    assert len(warned) == 1
    assert warned[0].levelno == logging.WARNING
    message = warned[0].getMessage()
    # the owner's sentence (2026-11-25), word for word, then what the unset value costs
    assert message.startswith(
        "ENVIRONMENT is not set: running with dev defaults; set ENVIRONMENT=prod "
        "for a deploy, ENVIRONMENT=dev to silence this."
    )
    assert "development token is not accepted" in message
    assert "SANDLOOM-B043" in message
    assert message == UNSET_ENVIRONMENT_WARNING


@pytest.mark.parametrize("value", [Environment.dev, Environment.test])
def test_set_explicitly_logs_no_environment_warning(
    value: Environment, caplog: pytest.LogCaptureFixture
) -> None:
    log = logging.getLogger("sandloom.app")
    with process_environment(value), caplog.at_level(logging.WARNING, "sandloom.app"):
        _warn_startup_configuration(log)
    assert not [r for r in caplog.records if "ENVIRONMENT" in r.getMessage()]


def _doctor_environment_rows(cfg: Settings) -> list[tuple[str, str]]:
    report = Report()
    check_config(report, cfg)
    return [
        (c.status, c.message)
        for c in report.checks
        if (c.section, c.name) == ("config", "environment")
    ]


def test_doctor_warns_when_environment_is_unset() -> None:
    [(status, message)] = _doctor_environment_rows(_cfg())
    assert status == WARN
    assert message == UNSET_ENVIRONMENT_WARNING  # the one text the app logs too


@pytest.mark.parametrize("value", ["dev", "test"])
def test_doctor_is_quiet_when_environment_is_set(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENVIRONMENT", value)
    assert _doctor_environment_rows(_cfg()) == []


def test_the_suite_runs_with_an_explicit_environment() -> None:
    """The root ``conftest.py`` writes ``ENVIRONMENT=dev`` when the shell leaves
    it unset: a contributor whose ``SANDLOOM_TEST_DATABASE_URL`` is not
    loopback (a devcontainer, a LAN host) must not fail SANDLOOM-B043 in every
    test that boots the app (review of env-unset-not-dev, M2). Fails where the
    seeding is removed and the shell sets nothing."""
    assert settings.environment_explicit
    assert environment_unset_problem(settings) is None


def test_the_helper_restores_the_mark() -> None:
    before = (settings.environment, settings.environment_explicit)
    with process_environment(Environment.prod):
        assert settings.environment_explicit and settings.is_prod
    with process_environment(None):
        assert not settings.environment_explicit and settings.is_dev
    assert (settings.environment, settings.environment_explicit) == before
