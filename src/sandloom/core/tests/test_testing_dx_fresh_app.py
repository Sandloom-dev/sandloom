"""What a freshly scaffolded app's first test runs hit (the DX audit of a new app).

Each test fails on the code that shipped before: a slot run refusing the
slot-less database ``migrate new`` stamped, a ``UserWarning`` about settings
nobody read, the developer's ``.env`` reaching the tests, ``as_user`` minting a
tenant that single-tenancy refuses, and a test run that dies with a traceback
when the test Redis is not there.
"""

from __future__ import annotations

import subprocess
import sys
import types
import uuid
from pathlib import Path

import jwt
import pytest

from sandloom.core.infra.errors import Unavailable
from sandloom.core.observability.unreachable import connection_failure
from sandloom.core.testing import auth, ownership, session_checks
from sandloom.core.testing.slots import slot_owner

# --------------------------------------------------------------------------- #
# 1. The owner of the database `migrate new` created
# --------------------------------------------------------------------------- #


def test_a_slot_run_is_the_owner_of_its_checkouts_slotless_database() -> None:
    """``migrate new`` creates the test database with no run slot, so its owner
    is the checkout alone (``<machine>:<path>``); the next run is slot 1."""
    slot = slot_owner("m1", "/work/app2", 1)
    assert slot == "m1:/work/app2#1"
    assert ownership.same_owner("m1:/work/app2", slot)  # what migrate new stamped
    assert ownership.same_owner("/work/app2", slot)  # a legacy bare-path marker
    assert ownership.same_owner(slot, slot)


def test_the_slotless_owner_never_extends_to_another_run() -> None:
    slot = slot_owner("m1", "/work/app2", 1)
    assert not ownership.same_owner("m1:/work/app3", slot)  # another checkout
    assert not ownership.same_owner("m2:/work/app2", slot)  # another machine
    assert not ownership.same_owner("m1:/work/app2#2", slot)  # another slot
    assert not ownership.same_owner("/work/app3", slot)
    assert not ownership.same_owner(None, slot)
    # One way only: a slot's database is not the slot-less run's.
    assert not ownership.same_owner(slot, "m1:/work/app2")


def test_an_explicit_owner_id_with_a_hash_is_not_cut() -> None:
    """Only ``<machine>:<path>#<digits>`` is a slot owner: an agent's own id
    stays what it was."""
    assert ownership.slotless_owner("agent#7") == "agent#7"
    assert ownership.slotless_owner("m1:/work/app2#1") == "m1:/work/app2"
    assert ownership.slotless_owner("m1:/work/app#2/x#3") == "m1:/work/app#2/x"
    assert not ownership.same_owner("agent", "agent#7")


# --------------------------------------------------------------------------- #
# 2 and 3. The run's environment, and the settings read before it
# --------------------------------------------------------------------------- #

_DEV_DOTENV = (
    "TENANCY_MODE=single\nSINGLE_TENANT_ID=11111111-1111-4111-8111-111111111111\n"
)
_TEST_ENV = (
    "ENVIRONMENT=test\n"
    "DATABASE_URL=postgresql+asyncpg://u:p@127.0.0.1:1/d\n"
    'INFRA=\'{"cache":{"default":{"url":"redis://127.0.0.1:1/0"}}}\'\n'
    "JWT_SECRET=dx-fresh-app-secret-0123456789abcdef0123456789\n"
)
_HOOK = (
    "import pathlib, types, warnings\n"
    "from sandloom.core.testing import plugin\n"
    "cfg = types.SimpleNamespace(rootpath=pathlib.Path.cwd())\n"
)


def _run(
    code: str, tmp_path: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """``code`` in a fresh interpreter whose working directory is ``tmp_path``
    and whose environment is ``env`` alone (a shell value never leaks in)."""
    return subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=tmp_path,
        env={"SANDLOOM_HOME": str(tmp_path / "home"), **env},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


@pytest.mark.subprocess_probe
def test_the_plugin_run_never_reads_the_developers_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(_DEV_DOTENV)
    (tmp_path / ".env.test").write_text(_TEST_ENV)
    read = "from sandloom.core.config.settings import settings\n"
    read += "print(settings.tenancy_mode)\n"
    # The control: without the plugin's hook the dev .env IS what settings read.
    control = _run(
        "from sandloom.core.testing.env import load_test_env\n"
        "import pathlib\n"
        "load_test_env([pathlib.Path.cwd()], create_database=False)\n" + read,
        tmp_path,
        {},
    )
    assert control.returncode == 0, control.stderr[-3000:]
    assert control.stdout.strip() == "single"
    # The pytest plugin's hook: shell, .env.test, .env.test.local and the
    # SANDLOOM_TEST_* overrides only.
    result = _run(
        _HOOK + "plugin.pytest_load_initial_conftests(cfg, None, [])\n" + read,
        tmp_path,
        {},
    )
    assert result.returncode == 0, result.stderr[-3000:]
    assert result.stdout.strip() == "multi"


@pytest.mark.subprocess_probe
@pytest.mark.parametrize(
    ("read_first", "warned"),
    [(False, "False"), (True, "True")],
    ids=["imported", "read"],
)
def test_the_late_settings_warning_means_the_settings_were_read(
    tmp_path: Path, read_first: bool, warned: str
) -> None:
    """The plugin imports the settings module (the kernel's imports reach it),
    which reads nothing: no warning. A settings object some earlier plugin or
    conftest used does warn: it keeps the environment of that moment."""
    (tmp_path / ".env.test").write_text(_TEST_ENV)
    code = (
        _HOOK
        + "import sandloom.core.config.settings as s\n"
        + ("s.settings.environment\n" if read_first else "")
        + "with warnings.catch_warnings(record=True) as seen:\n"
        "    warnings.simplefilter('always')\n"
        "    plugin.pytest_load_initial_conftests(cfg, None, [])\n"
        "print(any('were read before' in str(w.message) for w in seen))\n"
        "print(*[str(w.message) for w in seen if 'were read before' in str(w.message)])\n"
    )
    env = {
        # What the settings need to load before any .env.test is read.
        "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:1/d",
        "INFRA": '{"cache":{"default":{"url":"redis://127.0.0.1:1/0"}}}',
        "JWT_SECRET": "dx-fresh-app-secret-0123456789abcdef0123456789",
    }
    result = _run(code, tmp_path, env)
    assert result.returncode == 0, result.stderr[-3000:]
    first, _, message = result.stdout.partition("\n")
    assert first == warned, result.stdout
    if read_first:
        # The fix it names is the one plugin, listed first: never "combine" two.
        assert "-p sandloom.sdk.testing.plugin" in message
        assert "fixtures" not in message


# --------------------------------------------------------------------------- #
# 5. `as_user` under single tenancy
# --------------------------------------------------------------------------- #


def _tenant_of(headers: dict[str, str]) -> str:
    token = headers["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["tenant_ids"] == [claims["tenant_id"]]
    return str(claims["tenant_id"])


def test_a_minted_user_belongs_to_the_single_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.config.settings import settings

    single = uuid.uuid4()
    monkeypatch.setattr(settings, "tenancy_mode", "single")
    monkeypatch.setattr(settings, "single_tenant_id", single)
    _, headers = auth.make_user_headers()
    assert _tenant_of(headers) == str(single)
    assert auth.default_tenant_id() == single
    # A test that names a tenant still gets that one (a mismatch test).
    other = uuid.uuid4()
    assert _tenant_of(auth.make_user_headers(tenant_id=other)[1]) == str(other)
    # A tenant-less token stays tenant-less.
    _, bare = auth.make_user_headers(tenant=False)
    claims = jwt.decode(
        bare["Authorization"].removeprefix("Bearer "),
        options={"verify_signature": False},
    )
    assert "tenant_id" not in claims


def test_a_minted_user_gets_a_fresh_tenant_in_multi_tenancy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.config.settings import settings

    monkeypatch.setattr(settings, "tenancy_mode", "multi")
    monkeypatch.setattr(settings, "single_tenant_id", None)
    first = _tenant_of(auth.make_user_headers()[1])
    assert first != _tenant_of(auth.make_user_headers()[1])


def test_single_tenancy_without_its_tenant_is_refused_not_guessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.config.settings import settings

    monkeypatch.setattr(settings, "tenancy_mode", "single")
    monkeypatch.setattr(settings, "single_tenant_id", None)
    with pytest.raises(RuntimeError, match="SINGLE_TENANT_ID"):
        auth.make_user_headers()


# --------------------------------------------------------------------------- #
# 4. The test server does not answer (SANDLOOM-R014)
# --------------------------------------------------------------------------- #


def _unavailable(message: str, raw: BaseException) -> Unavailable:
    exc = Unavailable(message, capability="cache", instance="default")
    exc.__cause__ = raw
    return exc


def test_the_unreachable_line_names_the_server_to_start_yourself() -> None:
    """``dev up --services external`` starts nothing: the line names the server."""
    refused = ConnectionError(
        "Error 61 connecting to 127.0.0.1:26412. Connection refused."
    )
    line = connection_failure(
        _unavailable(
            "the test run's Redis server redis://127.0.0.1:26412 did not answer "
            "(ConnectionError)",
            refused,
        )
    )
    assert line is not None
    assert (
        "cannot reach the cache backend at 127.0.0.1:26412 (connection refused)" in line
    )
    assert (
        "uv run sandloom dev up (with --services external it starts nothing: " in line
    )
    assert "start the cache backend at 127.0.0.1:26412 yourself)" in line
    assert line.endswith("[SANDLOOM-R014]")


def test_a_server_named_by_url_gives_its_host_never_a_credential() -> None:
    named = connection_failure(
        _unavailable(
            "the test run's Redis server redis://cache.internal:6379 did not answer "
            "(ConnectionError)",
            ConnectionError("Error -2 connecting to cache.internal:6379."),
        )
    )
    assert named is not None and "at cache.internal:6379" in named
    # A "port" that is the password of user:1234@host is no address.
    credential = connection_failure(
        _unavailable(
            "redis://user:1234@cache.internal:6379 did not answer",
            ConnectionError("Error -2 connecting to cache.internal:6379."),
        )
    )
    assert credential is not None
    assert "1234" not in credential and "user" not in credential


def _stub_config(tmp_path: Path, workers: int) -> pytest.Config:
    return types.SimpleNamespace(  # type: ignore[return-value]
        rootpath=tmp_path,
        invocation_params=types.SimpleNamespace(args=()),
        option=types.SimpleNamespace(numprocesses=workers),
    )


def test_a_family_whose_server_does_not_answer_ends_the_session_with_r014(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ownership.OWNER_ENV, "dx-fresh-app-tests")
    monkeypatch.setattr(session_checks, "run_values", lambda config: {})

    def down(values: dict[str, str], count: int) -> None:
        raise _unavailable(
            "the test run's Redis server redis://127.0.0.1:26412 did not answer "
            "(ConnectionError)",
            ConnectionError(
                "Error 61 connecting to 127.0.0.1:26412. Connection refused."
            ),
        )

    monkeypatch.setattr(session_checks, "allocate_worker_resources", down)
    with pytest.raises(pytest.exit.Exception) as exited:
        session_checks.check_run_resources(_stub_config(tmp_path, 2))
    assert exited.value.returncode == pytest.ExitCode.USAGE_ERROR
    message = str(exited.value)
    assert "cannot reach the cache backend at 127.0.0.1:26412" in message
    assert "[SANDLOOM-R014]" in message


def test_the_session_exit_is_for_an_unreachable_server_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any other failure of a family keeps its traceback: nothing but
    ``Unavailable`` is an outage."""
    monkeypatch.setenv(ownership.OWNER_ENV, "dx-fresh-app-tests")
    monkeypatch.setattr(session_checks, "run_values", lambda config: {})

    def broken(values: dict[str, str], count: int) -> None:
        raise ValueError("a bug of the family")

    monkeypatch.setattr(session_checks, "allocate_worker_resources", broken)
    with pytest.raises(ValueError, match="a bug of the family"):
        session_checks.check_run_resources(_stub_config(tmp_path, 2))
