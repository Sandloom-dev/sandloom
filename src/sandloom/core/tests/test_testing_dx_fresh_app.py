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


_MACHINE = "abcdef123456"  # a machine id is 12 hex
_OTHER_MACHINE = "fedcba654321"


def test_a_slot_run_owns_the_slotless_database_nobody_claimed() -> None:
    """``migrate new`` creates the test database with no run slot, so its owner
    is the checkout alone (``<machine>:<path>``); the next run is slot 1. That
    holds while no run claimed the database: ``unclaimed`` says so."""
    slot = slot_owner(_MACHINE, Path("/work/app2"), 1)
    assert slot == f"{_MACHINE}:/work/app2#1"
    stamped = f"{_MACHINE}:/work/app2"  # what migrate new stamped
    assert ownership.same_owner(stamped, slot, unclaimed=True)
    assert ownership.same_owner("/work/app2", slot, unclaimed=True)  # legacy marker
    assert ownership.same_owner(slot, slot)


def test_a_slot_run_never_takes_a_slotless_database_a_run_claimed() -> None:
    """The slot-less holder of a CLAIMED database may be a live run of the same
    checkout (the one that got no slot): never the next run's."""
    slot = slot_owner(_MACHINE, Path("/work/app2"), 1)
    claimed = f"{_MACHINE}:/work/app2"
    assert not ownership.same_owner(claimed, slot)
    assert not ownership.same_owner(claimed, slot, unclaimed=False)
    assert not ownership.same_owner("/work/app2", slot)  # the legacy form too
    # The owner's own marker needs no claim check.
    assert ownership.same_owner(slot, slot)
    assert ownership.same_owner(claimed, claimed)
    # A slot-less owner is not a slot's: the legacy form stays as it was.
    assert ownership.same_owner("/work/app2", claimed)


def test_the_slotless_owner_never_extends_to_another_run() -> None:
    slot = slot_owner(_MACHINE, Path("/work/app2"), 1)
    for holder in (
        f"{_MACHINE}:/work/app3",  # another checkout
        f"{_OTHER_MACHINE}:/work/app2",  # another machine
        f"{_MACHINE}:/work/app2#2",  # another slot
        "/work/app3",
        None,
    ):
        assert not ownership.same_owner(holder, slot, unclaimed=True), holder
    # One way only: a slot's database is not the slot-less run's.
    assert not ownership.same_owner(slot, f"{_MACHINE}:/work/app2", unclaimed=True)


def test_an_explicit_owner_id_with_a_hash_is_not_cut() -> None:
    """Only ``<12 hex machine id>:<absolute path>#<digits>`` is a slot owner: an
    id a person or a CI job sets stays what it was, even one with a ``#``."""
    for explicit in ("agent#7", "ci:shard#2", "ci:/builds/app#2", "m1:/work/app#3"):
        assert ownership.slotless_owner(explicit) == explicit
        assert not ownership.same_owner("ci:shard", explicit, unclaimed=True)
    assert ownership.slotless_owner(f"{_MACHINE}:/work/app2#1") == (
        f"{_MACHINE}:/work/app2"
    )
    assert ownership.slotless_owner(f"{_MACHINE}:/work/app#2/x#3") == (
        f"{_MACHINE}:/work/app#2/x"
    )
    assert ownership.slotless_owner(f"{_MACHINE}:relative/app#1") == (
        f"{_MACHINE}:relative/app#1"
    )


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
def test_the_settings_a_module_declares_never_read_the_developers_dotenv(
    tmp_path: Path,
) -> None:
    """``declare()`` and ``settings_of`` read through the process snapshot, which
    a process whose ``Settings`` were never loaded builds on its own: from the
    same ``.env`` the ``Settings`` read, so none in a test run."""
    (tmp_path / ".env").write_text(_DEV_DOTENV)
    (tmp_path / ".env.test").write_text(_TEST_ENV)
    read = (
        "from sandloom.core.config.module_settings import process_snapshot\n"
        "print(process_snapshot().get('tenancy_mode'))\n"
    )
    control = _run(
        "from sandloom.core.testing.env import load_test_env\n"
        "import pathlib\n"
        "load_test_env([pathlib.Path.cwd()], create_database=False)\n" + read,
        tmp_path,
        {},
    )
    assert control.returncode == 0, control.stderr[-3000:]
    assert control.stdout.strip() == "('single', 'dotenv')"
    result = _run(
        _HOOK + "plugin.pytest_load_initial_conftests(cfg, None, [])\n" + read,
        tmp_path,
        {},
    )
    assert result.returncode == 0, result.stderr[-3000:]
    assert result.stdout.strip() == "(None, None)"


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
    # A password that merely STARTS with digits: no prefix of it either.
    prefixed = connection_failure(
        _unavailable(
            "postgresql://app:2024Secret@db.internal:5432/shop did not answer",
            ConnectionError("Error -2 connecting to db.internal:5432."),
        )
    )
    assert prefixed is not None
    assert "2024" not in prefixed and "Secret" not in prefixed
    assert "app:" not in prefixed


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
