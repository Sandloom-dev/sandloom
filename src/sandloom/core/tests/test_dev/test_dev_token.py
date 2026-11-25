"""``sandloom dev token``: when it refuses, and that it prints a usable token.

The command forges the provenance a token needs, so every way in is checked
twice and every refusal has a row that fails without it:

* the pure gates (:func:`~sandloom.core.cli.dev.token.local_refusal`,
  :func:`~sandloom.core.cli.dev.token.raw_refusal`): ``ENVIRONMENT`` unset, a
  typo, ``prod``, and a ``DATABASE_URL`` that is not this machine;
* the parent never starts the app's process for a refused request;
* the child process (``token_child``) refuses on the settings it loaded, even
  when run without the parent;
* CI, a bad argument, a crashing app, and no secret in any output.

The kernel mint is ``test_dev_token_mint.py`` one directory up.
"""

from __future__ import annotations

import io
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest

from sandloom.core.auth import dev_token as kernel_mint
from sandloom.core.auth import tokens as kernel_tokens
from sandloom.core.cli import dev as dev_cli
from sandloom.core.cli.dev import commands, identity, token

LOCAL_URL = "postgresql+asyncpg://dev:dev@127.0.0.1:5433/shop"
SECRET = secrets.token_urlsafe(48)


# --- the pure gates -------------------------------------------------------------
@pytest.mark.parametrize("environment", ["dev", "test"])
def test_dev_and_test_pass_the_gate(environment: str) -> None:
    assert token.local_refusal(environment, LOCAL_URL) is None


@pytest.mark.parametrize("environment", [None, ""])
def test_an_unset_environment_is_refused(environment: str | None) -> None:
    """The app would run as ``dev`` when ENVIRONMENT is not set; a tool that
    forges a credential does not guess."""
    why = token.local_refusal(environment, LOCAL_URL)
    assert why is not None and "ENVIRONMENT is not set" in why


@pytest.mark.parametrize(
    "environment",
    [
        *("Dev", "DEV", "Test", "prod", "production", "staging"),
        *(" dev", "dev ", "dev\n", "development", "dev,prod", "d", "x" * 500),
    ],
)
def test_a_typo_or_another_environment_is_refused(environment: str) -> None:
    why = token.local_refusal(environment, LOCAL_URL)
    assert why is not None and "ENVIRONMENT=" in why
    assert len(why) < 400  # the value is cut, not echoed whole


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@127.0.0.1:5432/db",
        "postgresql+asyncpg://u@127.9.9.9/db",
        "postgresql://u@[::1]:5432/db",
        "postgresql://u@localhost/db",
        "postgresql://u@LOCALHOST/db",
        "postgresql://u@/db?host=/var/run/postgresql",
        "postgresql://u@localhost/db?host=/tmp",
        "postgresql://u@127.0.0.1,localhost/db",
        "sqlite+aiosqlite:///./dev.db",
        "sqlite+aiosqlite:////tmp/dev.db",
        "postgresql://evil.example.com@127.0.0.1/db",  # the userinfo is not the host
    ],
)
def test_a_database_on_this_machine_passes(url: str) -> None:
    assert token.local_refusal("dev", url) is None


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@db.prod.example.com:5432/db",
        "postgresql://u:p@10.0.0.5/db",
        "postgresql://u:p@203.0.113.9:5432/db",
        "postgresql://u@[2001:db8::1]/db",
        "postgresql://u@0.0.0.0/db",
        "postgresql://u@localhost.evil.example.com/db",
        "postgresql://u@127.0.0.1.evil.example.com/db",
        "postgresql://127.0.0.1@evil.example.com/db",  # the userinfo is not the host
        "postgresql://127.0.0.1:5432@evil.example.com/db",
        "postgresql://u@localhost/db?host=db.example.com",
        "postgresql://u@localhost/db?hostaddr=203.0.113.9",
        "postgresql://u@127.0.0.1,db.example.com/db",
        "postgresql://u@db.example.com,127.0.0.1/db",
        "postgresql://u@localhost/db?host=/tmp,db.example.com",
        # The driver's parser, not ours: a `/` or `#` in the "password" ends the
        # authority for a hand-written split, and SQLAlchemy connects to the
        # host after the last `@` (here db.prod.example.com).
        "postgresql+asyncpg://localhost:pa/ss@db.prod.example.com/app",
        "postgresql+asyncpg://localhost:pa#ss@db.prod.example.com/app",
        "postgresql+asyncpg://127.0.0.1:pa?ss@db.prod.example.com/app",
        "postgresql://u@127.0.0.1/db?host=db.example.com&host=127.0.0.1",
    ],
)
def test_a_database_that_is_not_this_machine_is_refused(url: str) -> None:
    why = token.local_refusal("dev", url)
    assert why is not None and "not this machine" in why


NO_HOST_URL = "postgresql+asyncpg://u:p@/app"


@pytest.mark.parametrize(
    "pghost",
    ["db.prod.example.com", "10.0.0.5", "127.0.0.1,db.example.com", "2001:db8::1"],
)
def test_a_url_without_a_host_is_refused_for_a_remote_pghost(pghost: str) -> None:
    """The driver connects where ``PGHOST`` says when the url names no host."""
    why = token.local_refusal("dev", NO_HOST_URL, pghost)
    assert why is not None and "not this machine" in why


@pytest.mark.parametrize(
    "pghost", [None, "", "127.0.0.1", "localhost", "/var/run/postgresql", "::1"]
)
def test_a_url_without_a_host_passes_for_a_local_or_absent_pghost(
    pghost: str | None,
) -> None:
    assert token.local_refusal("dev", NO_HOST_URL, pghost) is None


@pytest.mark.parametrize(
    "url",
    [
        LOCAL_URL,
        "postgresql+asyncpg://u:p@/app?host=/var/run/postgresql",
        "sqlite+aiosqlite:///./dev.db",
    ],
)
def test_pghost_is_read_only_for_a_postgresql_url_that_names_no_host(url: str) -> None:
    """A url with a host (authority or query) or a file database never connects
    through ``PGHOST``: a remote one in the shell must not refuse it."""
    assert token.local_refusal("dev", url, "db.example.com") is None


def test_a_url_with_an_empty_authority_connects_where_pghost_says() -> None:
    """``postgresql+asyncpg:///app`` names no host either (no ``@``)."""
    url = "postgresql+asyncpg:///app"
    assert token.local_refusal("dev", url, "db.example.com") is not None
    assert token.local_refusal("dev", url, "127.0.0.1") is None
    assert token.local_refusal("dev", url, None) is None


def test_the_raw_check_reads_pghost_from_the_process_environment() -> None:
    base = {"ENVIRONMENT": "dev", "DATABASE_URL": NO_HOST_URL}
    assert token.raw_refusal(base, "") is None
    why = token.raw_refusal({**base, "PGHOST": "db.example.com"}, "")
    assert why is not None and "not this machine" in why
    # `.env` is not exported to the driver: only the process environment counts.
    assert token.raw_refusal(base, _dotenv(PGHOST="db.example.com")) is None


@pytest.mark.parametrize(
    ("name", "value", "text"),
    [("PGHOSTADDR", "10.0.0.5", "10.0.0.5"), ("PGSERVICE", "replica", "PGSERVICE")],
)
def test_the_raw_check_reads_the_other_libpq_variables_too(
    name: str, value: str, text: str
) -> None:
    """``sandloom migrate`` connects through libpq, so ``PGHOSTADDR`` and
    ``PGSERVICE`` of the process environment move the database like ``PGHOST``,
    and `.env` (not exported to the driver) does not."""
    base = {"ENVIRONMENT": "dev", "DATABASE_URL": NO_HOST_URL}
    why = token.raw_refusal({**base, name: value}, "")
    assert why is not None and "not this machine" in why and text in why
    assert token.raw_refusal(base, _dotenv(**{name: value})) is None


def test_a_local_pghostaddr_or_an_empty_service_passes() -> None:
    assert token.local_refusal("dev", NO_HOST_URL, pghostaddr="127.0.0.1") is None
    assert token.local_refusal("dev", NO_HOST_URL, pgservice="") is None
    assert token.local_refusal("dev", NO_HOST_URL, pghostaddr="10.0.0.5") is not None
    # an address of the url's own host moves it too: libpq connects to the address
    assert token.local_refusal("dev", LOCAL_URL, pghostaddr="10.0.0.5") is not None


@pytest.mark.parametrize("url", [None, "", "not a url", "//127.0.0.1/db", "127.0.0.1"])
def test_an_absent_or_unparsable_database_url_is_refused(url: str | None) -> None:
    why = token.local_refusal("dev", url)
    assert why is not None and "DATABASE_URL" in why


def test_a_refusal_never_echoes_the_urls_password() -> None:
    why = token.local_refusal("dev", "postgresql://app:s3cretpw@db.example.com/x")
    assert why is not None
    assert "s3cretpw" not in why and "postgresql://" not in why
    assert "db.example.com" in why  # the host is what the developer must fix


def test_the_environment_is_judged_before_the_database() -> None:
    why = token.local_refusal("prod", "postgresql://u@db.example.com/x")
    assert why is not None and "ENVIRONMENT=" in why


def test_the_cli_and_the_kernel_mint_agree_on_the_dev_environments() -> None:
    assert (
        token.DEV_ENVIRONMENTS
        == kernel_mint.DEV_ENVIRONMENTS
        == kernel_tokens.DEV_ENVIRONMENTS
        == ("dev", "test")
    )


# --- the raw values: the process environment, then .env -----------------------
def _dotenv(**values: str) -> str:
    return "".join(f"{key}={value}\n" for key, value in values.items())


def test_the_environment_wins_over_dotenv_as_the_app_reads_them() -> None:
    dotenv = _dotenv(ENVIRONMENT="prod", DATABASE_URL="postgresql://u@db.example.com/x")
    assert token.raw_refusal({}, dotenv) is not None
    exported = {"ENVIRONMENT": "dev", "DATABASE_URL": LOCAL_URL}
    assert token.raw_refusal(exported, dotenv) is None


def test_dotenv_alone_can_carry_both_values() -> None:
    assert (
        token.raw_refusal({}, _dotenv(ENVIRONMENT="dev", DATABASE_URL=LOCAL_URL))
        is None
    )
    assert token.raw_refusal({}, _dotenv(ENVIRONMENT="dev")) is not None  # no database
    assert token.raw_refusal({}, _dotenv(DATABASE_URL=LOCAL_URL)) is not None  # unset
    assert token.raw_refusal({}, "") is not None


@pytest.mark.parametrize(
    ("text", "refused"),
    [
        # `.env` names are read as the settings reader reads them: any case.
        (f"environment=prod\nDATABASE_URL={LOCAL_URL}\n", True),
        (f"Environment=prod\ndatabase_url={LOCAL_URL}\n", True),
        ("ENVIRONMENT=dev\ndatabase_url=postgresql://u@db.example.com/x\n", True),
        (f"environment=dev\nDatabase_Url={LOCAL_URL}\n", False),
        # Both spellings: the refused value decides.
        (f"ENVIRONMENT=dev\nenvironment=prod\nDATABASE_URL={LOCAL_URL}\n", True),
        (f"environment=prod\nENVIRONMENT=dev\nDATABASE_URL={LOCAL_URL}\n", True),
        # An entry with no value is no entry.
        (f"ENVIRONMENT\nDATABASE_URL={LOCAL_URL}\n", True),
        (f"ENVIRONMENT=dev\nDATABASE_URL\ndatabase_url={LOCAL_URL}\n", False),
    ],
)
def test_dotenv_names_are_case_insensitive_as_the_settings_reader_is(
    text: str, refused: bool
) -> None:
    assert (token.raw_refusal({}, text) is not None) is refused


@pytest.mark.parametrize(
    "line", ['ENVIRONMENT="Dev"', "ENVIRONMENT='prod'", "export ENVIRONMENT=prod"]
)
def test_dotenv_quotes_and_export_do_not_hide_a_refused_value(line: str) -> None:
    assert token.raw_refusal({}, f"{line}\nDATABASE_URL={LOCAL_URL}\n") is not None


@pytest.mark.parametrize(
    "line", ['ENVIRONMENT="dev"', "export ENVIRONMENT=dev", "ENVIRONMENT=dev # local"]
)
def test_dotenv_quotes_export_and_comments_read_as_the_app_reads_them(
    line: str,
) -> None:
    assert token.raw_refusal({}, f"{line}\nDATABASE_URL={LOCAL_URL}\n") is None


def test_the_variable_name_is_case_insensitive_as_the_settings_reader_is() -> None:
    """``environment=prod`` is read by the app as ``ENVIRONMENT=prod``: it
    must not slip past a gate that looks only at the upper-case name, and
    when both spellings are exported the refused one decides."""
    base = {"DATABASE_URL": LOCAL_URL}
    assert token.raw_refusal({**base, "environment": "prod"}, "") is not None
    assert token.raw_refusal({**base, "Environment": "dev"}, "") is None
    both = {**base, "ENVIRONMENT": "dev", "environment": "prod"}
    assert token.raw_refusal(both, "") is not None
    remote = {"ENVIRONMENT": "dev", "database_url": "postgresql://u@db.example.com/x"}
    assert token.raw_refusal(remote, "") is not None


# --- the parent never starts the app's process for a refused request -----------
def _ctx(
    root: Path, environ: dict[str, str], *, verbose: bool = False
) -> commands.Context:
    return commands.Context(
        identity=identity.resolve(root, environ),
        environ=environ,
        out=io.StringIO(),
        err=io.StringIO(),
        verbose=verbose,
    )


def _app(root: Path, source: str) -> None:
    (root / "shopapp").mkdir(exist_ok=True)
    (root / "shopapp" / "__init__.py").write_text("")
    (root / "shopapp" / "asgi.py").write_text(source)


#: A composition root whose claim policy assigns roles (identity) and tenant
#: grants (tenancy), built as ``create_app`` does from a root's map.
_POLICY_APP = (
    "from sandloom.core.app_factory.claim_authorities import (\n"
    "    build_claim_authorities, filter_to_composition)\n"
    "class _Core:\n"
    "    loaded_modules = ()\n"
    "    claim_authorities = filter_to_composition(\n"
    "        build_claim_authorities({\n"
    '            "issuer": "shopapp.modules.identity",\n'
    '            "roles": "shopapp.modules.identity",\n'
    '            "tenant_ids": "shopapp.modules.tenancy",\n'
    "        }),\n"
    "        loaded={}, bindings={})\n"
    "    def get_service(self, name):\n"
    "        raise KeyError(name)\n"
    "core = _Core()\n"
)
#: A bare app: no module assigns a claim authority.
_BARE_APP = (
    "from sandloom.core.claims import NO_AUTHORITIES\n"
    "class _Core:\n"
    "    loaded_modules = ()\n"
    "    claim_authorities = NO_AUTHORITIES\n"
    "    def get_service(self, name):\n"
    "        raise KeyError(name)\n"
    "core = _Core()\n"
)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "shop"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "shop"\n')
    _app(root, _POLICY_APP)
    return root


@pytest.fixture
def environ(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv("SANDLOOM_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PYTHONSAFEPATH", raising=False)
    owned = {
        "CI",
        "DOCKER_HOST",
        "PYTHONSAFEPATH",
        "ENVIRONMENT",
        "DATABASE_URL",
        "PGHOST",
        "PGHOSTADDR",
        "PGSERVICE",
        "JWT_SECRET",
        "JWT_PRIVATE_KEY",
        "JWT_PUBLIC_KEY",
        "INFRA",
        "SECRETS_DIR",
        "SANDLOOM_COMPOSITION_ROOT",
        "SANDLOOM_DEBUG",
        "TENANCY_MODE",
        "SINGLE_TENANT_ID",
        "ACCESS_TOKEN_EXPIRE_MINUTES",
    }
    env = {k: v for k, v in os.environ.items() if k.upper() not in owned}
    env.update(
        ENVIRONMENT="dev",
        DATABASE_URL=LOCAL_URL,
        JWT_SECRET=SECRET,
        SANDLOOM_COMPOSITION_ROOT="shopapp.asgi",
    )
    return env


@pytest.fixture
def no_child(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Any start of the app's process fails the test: a refused request must
    never reach it."""
    started: list[str] = []

    def fail(*args: Any, **kwargs: Any) -> None:
        started.append("started")
        raise AssertionError("the app's process was started for a refused request")

    monkeypatch.setattr(token, "_child_answer", fail)
    return started


@pytest.mark.parametrize(
    ("change", "text"),
    [
        ({"ENVIRONMENT": None}, "ENVIRONMENT is not set"),
        ({"ENVIRONMENT": "Dev"}, "ENVIRONMENT='Dev'"),
        ({"ENVIRONMENT": "prod"}, "ENVIRONMENT='prod'"),
        ({"ENVIRONMENT": "production"}, "ENVIRONMENT='production'"),
        ({"DATABASE_URL": None}, "DATABASE_URL is not set"),
        ({"DATABASE_URL": "postgresql://u:pw@db.example.com/x"}, "not this machine"),
        ({"DATABASE_URL": "nonsense"}, "does not parse"),
        (
            {
                "DATABASE_URL": "postgresql+asyncpg://localhost:pa/ss@db.prod.example.com/x"
            },
            "db.prod.example.com",
        ),
        (
            {
                "DATABASE_URL": "postgresql+asyncpg://u:pw@/x",
                "PGHOST": "pg.example.com",
            },
            "pg.example.com",
        ),
        (
            {
                "DATABASE_URL": "postgresql+asyncpg://u:pw@/x",
                "PGHOSTADDR": "10.0.0.5",
            },
            "10.0.0.5",
        ),
        (
            {"DATABASE_URL": "postgresql+asyncpg://u:pw@/x", "PGSERVICE": "replica"},
            "PGSERVICE",
        ),
    ],
)
def test_a_refused_request_never_starts_the_app(
    checkout: Path,
    environ: dict[str, str],
    no_child: list[str],
    change: dict[str, str | None],
    text: str,
) -> None:
    for name, value in change.items():
        if value is None:
            environ.pop(name)
        else:
            environ[name] = value
    ctx = _ctx(checkout, environ)
    with pytest.raises(commands.DevError, match=text):
        token.run(ctx, token.Request())
    assert no_child == []
    assert ctx.out.getvalue() == ""  # no token on stdout


def test_a_copied_production_dotenv_that_says_dev_is_refused(
    checkout: Path, environ: dict[str, str], no_child: list[str]
) -> None:
    """The case the database host exists for: ENVIRONMENT=dev is true of the
    file, the database is the production one."""
    for name in ("ENVIRONMENT", "DATABASE_URL", "JWT_SECRET"):
        environ.pop(name)
    (checkout / ".env").write_text(
        _dotenv(
            ENVIRONMENT="dev",
            DATABASE_URL="postgresql+asyncpg://app:s3cretpw@prod-db.internal:5432/app",
            JWT_SECRET=SECRET,
        )
    )
    with pytest.raises(commands.DevError, match="prod-db.internal") as refused:
        token.run(_ctx(checkout, environ), token.Request())
    assert "s3cretpw" not in str(refused.value) and SECRET not in str(refused.value)
    assert no_child == []


def test_ci_is_refused_before_anything(
    checkout: Path, environ: dict[str, str], no_child: list[str]
) -> None:
    environ["CI"] = "true"
    with pytest.raises(commands.DevError, match="CI is set"):
        token.run(_ctx(checkout, environ), token.Request())
    assert no_child == []


@pytest.mark.parametrize(
    ("value", "is_ci"),
    [
        *(("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True)),
        *(("0", False), ("false", False), ("no", False), ("off", False), ("", False)),
    ],
)
def test_ci_means_a_true_value_as_the_docs_say(value: str, is_ci: bool) -> None:
    """The docs, the ADR and the changelog say "CI set to a true value (`1`,
    `true`, `yes` or `on`)": `CI=false` is a developer machine."""
    assert commands.in_ci({"CI": value}) is is_ci


def test_the_cli_refuses_in_ci_with_its_own_text(
    checkout: Path,
    environ: dict[str, str],
    no_child: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(checkout)
    environ["CI"] = "1"
    assert dev_cli.main(["token"], environ=environ) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert token.CI_REFUSAL in captured.err
    assert "SANDLOOM_TEST_DATABASE_URL" not in captured.err  # not the `up` advice
    assert no_child == []


@pytest.mark.parametrize(
    "args",
    [
        ["--user", "not-a-uuid"],
        ["--tenant", "42"],
        ["--ttl-minutes", "soon"],
        ["--unknown"],
    ],
)
def test_a_bad_argument_is_a_usage_error_before_anything_runs(
    args: list[str], no_child: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exited:
        dev_cli.main(["token", *args], environ={})
    assert exited.value.code == 2
    assert no_child == []
    assert capsys.readouterr().out == ""


# --- the child process ---------------------------------------------------------------
def _decode(text: str) -> dict[str, Any]:
    return jwt.decode(
        text.strip(), SECRET, algorithms=["HS256"], options={"verify_aud": False}
    )


def test_it_prints_a_token_the_app_signed_and_what_it_carries(
    checkout: Path, environ: dict[str, str]
) -> None:
    tenant = uuid4()
    ctx = _ctx(checkout, environ)
    request = token.Request(
        tenant=tenant, roles=("viewer", "admin"), ttl_minutes=5, user=None
    )
    assert token.run(ctx, request) == 0
    claims = _decode(ctx.out.getvalue())
    assert ctx.out.getvalue().count("\n") == 1  # the token and nothing else
    assert claims["sub"] == str(token.DEV_USER_ID)
    assert claims["roles"] == ["admin", "viewer"]
    assert claims["tenant_ids"] == [str(tenant)]
    assert claims["claims_sources"] == ["identity", "tenancy"]
    assert claims["dev_mint"] is True  # the marker a production verifier refuses
    assert claims["exp"] - claims["iat"] == 5 * 60
    summary = ctx.err.getvalue()
    assert str(token.DEV_USER_ID) in summary and str(tenant) in summary
    assert "admin, viewer" in summary and "valid 5 minutes" in summary
    assert SECRET not in ctx.out.getvalue() + summary


def test_the_user_is_the_one_asked_for(checkout: Path, environ: dict[str, str]) -> None:
    ctx = _ctx(checkout, environ)
    user = uuid4()
    token.run(ctx, token.Request(user=user))
    assert UUID(_decode(ctx.out.getvalue())["sub"]) == user


def test_quiet_prints_the_token_only(checkout: Path, environ: dict[str, str]) -> None:
    ctx = _ctx(checkout, environ)
    token.run(ctx, token.Request(quiet=True))
    assert _decode(ctx.out.getvalue())["token_type"] == "access"
    assert ctx.err.getvalue() == ""


def test_dotenv_alone_is_enough(checkout: Path, environ: dict[str, str]) -> None:
    for name in ("ENVIRONMENT", "DATABASE_URL", "JWT_SECRET"):
        environ.pop(name)
    (checkout / ".env").write_text(
        _dotenv(ENVIRONMENT="dev", DATABASE_URL=LOCAL_URL, JWT_SECRET=SECRET)
    )
    ctx = _ctx(checkout, environ)
    assert token.run(ctx, token.Request()) == 0
    assert _decode(ctx.out.getvalue())["sub"] == str(token.DEV_USER_ID)


def test_the_main_entry_prints_the_token_on_stdout_and_notes_on_stderr(
    checkout: Path,
    environ: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(checkout)
    argv = ["token", "--role", "admin", "--role", "admin", "--ttl-minutes", "7"]
    assert dev_cli.main(argv, environ=environ) == 0
    captured = capsys.readouterr()
    claims = _decode(captured.out)
    assert claims["roles"] == ["admin"] and claims["exp"] - claims["iat"] == 7 * 60
    assert "dev token for user" in captured.err
    assert "no --tenant" in captured.err  # the policy assigns tenant grants
    assert SECRET not in captured.out + captured.err


def test_a_claim_nobody_assigns_is_refused_with_the_way_out(
    checkout: Path, environ: dict[str, str]
) -> None:
    _app(checkout, _BARE_APP)
    ctx = _ctx(checkout, environ)
    with pytest.raises(commands.DevError, match="claim_authorities") as refused:
        token.run(ctx, token.Request(roles=("admin",)))
    assert "--role" in str(refused.value)
    assert ctx.out.getvalue() == ""
    with pytest.raises(commands.DevError, match="--tenant"):
        token.run(_ctx(checkout, environ), token.Request(tenant=uuid4()))


def test_a_bare_app_gets_a_token_and_the_multi_tenancy_note(
    checkout: Path, environ: dict[str, str]
) -> None:
    _app(checkout, _BARE_APP)
    environ["TENANCY_MODE"] = "multi"
    ctx = _ctx(checkout, environ)
    token.run(ctx, token.Request())
    assert _decode(ctx.out.getvalue())["claims_sources"] == []
    assert "TENANCY_MODE=single" in ctx.err.getvalue()
    environ.update(TENANCY_MODE="single", SINGLE_TENANT_ID=str(uuid4()))
    single = _ctx(checkout, environ)
    token.run(single, token.Request())
    assert "TENANCY_MODE" not in single.err.getvalue()


def test_a_single_tenant_app_gets_no_tenant_advice_even_with_a_tenant_authority(
    checkout: Path, environ: dict[str, str]
) -> None:
    """The policy of ``checkout`` assigns tenant grants, so a multi-tenant app
    is told to pass ``--tenant``; under ``TENANCY_MODE=single`` the configured
    tenant binds for every token and that advice would mislead."""
    multi = _ctx(checkout, {**environ, "TENANCY_MODE": "multi"})
    token.run(multi, token.Request())
    assert "no --tenant" in multi.err.getvalue()
    single = _ctx(
        checkout,
        {**environ, "TENANCY_MODE": "single", "SINGLE_TENANT_ID": str(uuid4())},
    )
    token.run(single, token.Request())
    assert "note:" not in single.err.getvalue()


@pytest.mark.parametrize("ttl", [0, -5, kernel_mint.MAX_TTL_MINUTES + 1])
def test_a_ttl_out_of_bounds_is_refused_by_the_mint(
    checkout: Path, environ: dict[str, str], ttl: int
) -> None:
    ctx = _ctx(checkout, environ)
    with pytest.raises(commands.DevError, match="minutes"):
        token.run(ctx, token.Request(ttl_minutes=ttl))
    assert ctx.out.getvalue() == ""


def test_a_bad_role_is_refused_by_the_mint(
    checkout: Path, environ: dict[str, str]
) -> None:
    with pytest.raises(commands.DevError, match="not a role name"):
        token.run(_ctx(checkout, environ), token.Request(roles=("a b",)))


def _child(root: Path, environ: dict[str, str]) -> list[dict[str, Any]]:
    """The answers of the child process run on its own, without the parent."""
    request = {"user": str(uuid4()), "tenant": None, "roles": [], "ttl_minutes": None}
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "sandloom.core.cli.dev.token_child",
            json.dumps(request),
        ],
        env=environ,
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return [
        json.loads(line.removeprefix(token.SENTINEL))
        for line in done.stdout.splitlines()
        if line.startswith(token.SENTINEL)
    ]


def test_the_child_alone_mints_in_dev(checkout: Path, environ: dict[str, str]) -> None:
    (answer,) = _child(checkout, environ)
    assert "token" in answer and "refused" not in answer


@pytest.mark.parametrize(
    "change",
    [
        {"ENVIRONMENT": "prod"},
        {"ENVIRONMENT": "Dev"},
        {"DATABASE_URL": "postgresql+asyncpg://u:pw@db.example.com:5432/x"},
        {"DATABASE_URL": "postgresql+asyncpg://u:pw@localhost/x?host=db.example.com"},
        {"DATABASE_URL": "postgresql+asyncpg://localhost:pa/ss@db.prod.example.com/x"},
        {"DATABASE_URL": "postgresql+asyncpg://u:pw@/x", "PGHOST": "pg.example.com"},
        {"DATABASE_URL": "postgresql+asyncpg://u:pw@/x", "PGHOSTADDR": "10.0.0.5"},
        {"DATABASE_URL": "postgresql+asyncpg://u:pw@/x", "PGSERVICE": "replica"},
    ],
)
def test_the_child_refuses_on_its_own_loaded_settings(
    checkout: Path, environ: dict[str, str], change: dict[str, str]
) -> None:
    """Whatever the parent let through, the process that signs checks the
    settings it loaded, so a caller that skips the parent is refused too."""
    (answer,) = _child(checkout, {**environ, **change})
    assert "token" not in answer
    assert isinstance(answer["refused"], str)
    assert SECRET not in answer["refused"] and "pw@" not in answer["refused"]


def test_invalid_settings_are_refused_without_echoing_the_secret(
    checkout: Path, environ: dict[str, str]
) -> None:
    # A validation error echoes the input that failed it, a secret's included.
    environ["ACCESS_TOKEN_EXPIRE_MINUTES"] = "leaky-marker-s3cret"
    ctx = _ctx(checkout, environ, verbose=True)
    with pytest.raises(commands.DevError, match="settings do not load") as refused:
        token.run(ctx, token.Request())
    assert "leaky-marker" not in str(refused.value)
    assert ctx.out.getvalue() == "" and "leaky-marker" not in ctx.err.getvalue()


def test_a_missing_composition_root_is_refused_naming_the_fix(
    checkout: Path, environ: dict[str, str]
) -> None:
    environ["SANDLOOM_COMPOSITION_ROOT"] = "nothere.asgi"
    with pytest.raises(commands.DevError, match="composition root.*nothere"):
        token.run(_ctx(checkout, environ), token.Request())


def test_an_app_that_prints_at_import_and_at_exit_does_not_corrupt_the_answer(
    checkout: Path, environ: dict[str, str]
) -> None:
    _app(
        checkout,
        "import atexit, sys\n"
        "sys.stdout.write('banner without a newline')\n"
        "atexit.register(lambda: print('{\"goodbye\": true}'))\n" + _BARE_APP,
    )
    ctx = _ctx(checkout, environ)
    assert token.run(ctx, token.Request()) == 0
    assert _decode(ctx.out.getvalue())["token_type"] == "access"


def test_the_checkout_package_is_found_under_pythonsafepath(
    checkout: Path, environ: dict[str, str]
) -> None:
    environ["PYTHONSAFEPATH"] = "1"
    ctx = _ctx(checkout, environ)
    assert token.run(ctx, token.Request()) == 0


_LEAKY = (
    'raise RuntimeError("cannot reach redis://app:s3cretpw@cache.internal:6379/0")\n'
)


def test_a_crashing_app_names_the_exit_code_and_echoes_nothing(
    checkout: Path, environ: dict[str, str]
) -> None:
    _app(checkout, _LEAKY)
    ctx = _ctx(checkout, environ)
    with pytest.raises(commands.DevError) as refused:
        token.run(ctx, token.Request())
    message = str(refused.value)
    assert "exited with code 1" in message and "--debug" in message
    assert "s3cretpw" not in message and "cache.internal" not in message
    assert ctx.out.getvalue() == ""


def test_debug_shows_the_last_error_line_without_any_url(
    checkout: Path, environ: dict[str, str]
) -> None:
    _app(checkout, _LEAKY)
    with pytest.raises(commands.DevError) as refused:
        token.run(_ctx(checkout, environ, verbose=True), token.Request())
    message = str(refused.value)
    assert "cannot reach" in message
    assert "s3cretpw" not in message and "cache.internal" not in message


def test_a_child_that_answers_nothing_is_refused(
    checkout: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(token, "SENTINEL", "OTHER:")
    with pytest.raises(commands.DevError, match="printed no answer"):
        token.run(_ctx(checkout, environ), token.Request())


def test_the_kernel_mint_is_checked_again_in_the_child(
    checkout: Path, environ: dict[str, str]
) -> None:
    """A fake composition root that flips the environment after the child's
    own gate ran (it imports before the mint): the kernel's floor check still
    refuses, so no gate depends on another having run."""
    _app(
        checkout,
        "from sandloom.core.config.common import Environment\n"
        "from sandloom.core.config.settings import settings\n"
        "settings.environment = Environment.prod\n" + _BARE_APP,
    )
    with pytest.raises(commands.DevError, match="ENVIRONMENT=prod"):
        token.run(_ctx(checkout, environ), token.Request())
