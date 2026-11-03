"""``sandloom admin create`` / ``invite`` and ``grant-admin``'s take-over rule.

The ``identity.security.v1`` contract is faked here; identity's real one is
covered in ``sandloom_identity/tests/api/test_first_admin.py``.
"""

from __future__ import annotations

import io
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sandloom.core.cli import admin
from sandloom.core.cli._operator import OperatorIntent
from sandloom.core.cli.security import SECURITY_CONTRACT, grant_admin
from sandloom.core.config.settings import settings
from sandloom.core.module_api._errors import ContractCallError
from sandloom.core.module_api._service_registry import ServiceNotFound

TOKEN = "tok.en.sig"
GOOD = "Long-Enough-Passw0rd!"


class _Security:
    def __init__(self, **accounts: SimpleNamespace) -> None:
        self.accounts = {
            k.replace("_at_", "@").replace("_", "."): v for k, v in accounts.items()
        }
        self.calls: list[tuple[Any, ...]] = []

    async def account_standing(self, email: str) -> Any:
        return self.accounts.get(email)

    async def password_problem(self, email: str, password: str) -> str | None:
        return "Password is too short" if len(password) < 12 else None

    async def create_platform_admin(
        self, email: str, full_name: str, reason: str, operator: str, key: str
    ) -> uuid.UUID:
        self.calls.append(("create", email, full_name, reason, operator, key))
        if email in self.accounts:
            raise ContractCallError("already exists", status_code=409)
        user_id = uuid.uuid4()
        self.accounts[email] = SimpleNamespace(
            user_id=user_id, is_platform_admin=True, email_verified=True, is_active=True
        )
        return user_id

    async def take_over_platform_admin(
        self, user_id: uuid.UUID, reason: str, operator: str, key: str
    ) -> bool:
        self.calls.append(("take_over", user_id, reason, operator, key))
        return True

    async def reissue_set_password(
        self, user_id: uuid.UUID, reason: str, operator: str, key: str
    ) -> bool:
        self.calls.append(("reissue", user_id, reason, operator, key))
        return True

    async def set_password_token(self, user_id: uuid.UUID) -> str | None:
        self.calls.append(("mint", user_id))
        return TOKEN

    async def set_password_with_token(
        self, token: str, new_password: str, key: str
    ) -> bool:
        self.calls.append(("set_password", token, new_password, key))
        return True

    async def find_user_id(self, email: str) -> uuid.UUID | None:
        found = self.accounts.get(email)
        return found.user_id if found else None

    async def appoint_platform_admin(
        self, user_id: uuid.UUID, reason: str, operator: str, key: str
    ) -> bool:
        self.calls.append(("appoint", user_id))
        return True

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


class _Core:
    def __init__(self, security: Any) -> None:
        self.security = security

    def get_service(self, name: str) -> Any:
        if name != SECURITY_CONTRACT or self.security is None:
            raise ServiceNotFound(name)
        return self.security


def _account(*, admin_role: bool, verified: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=uuid.uuid4(),
        is_platform_admin=admin_role,
        email_verified=verified,
        is_active=True,
    )


class _Stream(io.StringIO):
    def __init__(self, text: str = "", *, tty: bool) -> None:
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture
def run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> Any:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "frontend_url", "http://localhost:3000")
    monkeypatch.setattr(settings, "admin_console_enabled", None)
    fields = set(settings.model_fields_set) - {"frontend_url"}
    monkeypatch.setattr(type(settings), "model_fields_set", property(lambda s: fields))

    def _run(
        security: Any, *argv: str, stdin: str = "", tty: bool = False
    ) -> tuple[int, str, str]:
        monkeypatch.setattr(admin, "_build_core", lambda: _Core(security))
        monkeypatch.setattr(admin.sys, "stdin", _Stream(stdin, tty=tty))
        monkeypatch.setattr(admin, "_stdout_is_tty", lambda: tty)
        try:
            code = admin.main(list(argv))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
            if isinstance(exc.code, str):
                print(exc.code, file=admin.sys.stderr)
        out = capsys.readouterr()
        return code, out.out, out.err

    return _run


def _prod(monkeypatch: pytest.MonkeyPatch, frontend: str | None = None) -> None:
    monkeypatch.setattr(type(settings), "is_prod", property(lambda s: True))
    if frontend is not None:
        monkeypatch.setattr(settings, "frontend_url", frontend)
        fields = set(settings.model_fields_set) | {"frontend_url"}
        monkeypatch.setattr(
            type(settings), "model_fields_set", property(lambda s: fields)
        )


def test_no_terminal_and_no_mode_names_both_flags(run: Any) -> None:
    security = _Security()
    code, _, err = run(security, "create", "--email", "owner@example.com")
    assert code == 2
    assert "--invite" in err and "--password-stdin" in err
    assert security.calls == []


def test_invite_creates_and_prints_a_fragment_link(run: Any) -> None:
    security = _Security()
    code, out, _ = run(security, "create", "--email", "owner@example.com", "--invite")
    assert code == 0
    assert security.names() == ["create", "mint"]
    assert "Administrator owner@example.com created" in out
    assert f"http://127.0.0.1:8000/admin/set-password#token={TOKEN}" in out
    assert "uv run sandloom admin invite --email owner@example.com" in out
    create = security.calls[0]
    assert create[3] == "first administrator"


def test_the_link_uses_this_checkouts_dev_run_port(run: Any, tmp_path: Path) -> None:
    (tmp_path / ".sandloom" / "run").mkdir(parents=True)
    (tmp_path / ".sandloom" / "run" / "app.json").write_text(json.dumps({"port": 8123}))
    code, out, _ = run(_Security(), "create", "--email", "o@example.com", "--invite")
    assert code == 0
    assert "http://127.0.0.1:8123/admin/set-password#token=" in out


def test_if_missing_exits_zero_only_for_an_existing_admin(run: Any) -> None:
    security = _Security(owner_at_example_com=_account(admin_role=True))
    argv = ("create", "--email", "owner@example.com", "--invite", "--if-missing")
    code, out, _ = run(security, *argv)
    assert code == 0 and "already" in out
    assert security.calls == []

    member = _Security(owner_at_example_com=_account(admin_role=False))
    code, _, err = run(member, *argv)
    assert code == 1
    assert "grant-admin" in err
    assert member.calls == []


def test_an_existing_admin_without_if_missing_points_to_invite(run: Any) -> None:
    security = _Security(owner_at_example_com=_account(admin_role=True))
    code, _, err = run(security, "create", "--email", "owner@example.com", "--invite")
    assert code == 1
    assert "already exists" in err and "sandloom admin invite" in err


def test_an_unverified_account_points_to_take_over(run: Any) -> None:
    squatted = _account(admin_role=False, verified=False)
    security = _Security(owner_at_example_com=squatted)
    code, _, err = run(security, "create", "--email", "owner@example.com", "--invite")
    assert code == 1 and "--take-over" in err

    code, out, _ = run(
        security, "create", "--email", "owner@example.com", "--invite", "--take-over"
    )
    assert code == 0
    assert security.names() == ["take_over", "mint"]
    assert security.calls[0][1] == squatted.user_id
    assert "set-password#token=" in out


def test_a_lost_create_race_counts_as_existing(run: Any) -> None:
    class _Racing(_Security):
        async def create_platform_admin(self, *args: Any) -> uuid.UUID:
            self.accounts["owner@example.com"] = _account(admin_role=True)
            raise ContractCallError("already exists", status_code=409)

    argv = ("create", "--email", "owner@example.com", "--invite", "--if-missing")
    code, _, _ = run(_Racing(), *argv)
    assert code == 0


def test_production_needs_apply_confirm_and_reason(
    run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prod(monkeypatch, "https://app.example.com")
    security = _Security()
    base = ("create", "--email", "owner@example.com", "--invite")
    for extra in ((), ("--apply",), ("--apply", "--confirm-production")):
        code, _, err = run(security, *base, *extra)
        assert code == 2, extra
        assert "--apply --confirm-production --reason" in err
    assert security.calls == []
    code, out, _ = run(
        security, *base, "--apply", "--confirm-production", "--reason", "go-live"
    )
    assert code == 0
    assert security.calls[0][3] == "go-live"
    # The console is off in production by default: the frontend's reset page.
    assert f"https://app.example.com/auth/reset-password?token={TOKEN}" in out


def test_production_invite_needs_frontend_url(
    run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prod(monkeypatch)
    flags = ("--apply", "--confirm-production", "--reason", "go-live")
    security = _Security()
    code, _, err = run(
        security, "create", "--email", "owner@example.com", "--invite", *flags
    )
    assert code == 2 and "FRONTEND_URL" in err
    assert security.calls == []


def test_password_stdin_sets_the_password_through_the_token(run: Any) -> None:
    security = _Security()
    code, out, _ = run(
        security,
        "create",
        "--email",
        "owner@example.com",
        "--password-stdin",
        stdin=GOOD + "\n",
    )
    assert code == 0, out
    assert security.names() == ["create", "mint", "set_password"]
    _, token, password, key = security.calls[2]
    assert (token, password) == (TOKEN, GOOD)
    assert GOOD not in key and TOKEN not in key
    for call in security.calls[:2]:
        assert GOOD not in repr(call)
    assert "set-password" not in out
    assert "can sign in" in out


def test_password_stdin_refuses_a_terminal_and_a_weak_password(run: Any) -> None:
    argv = ("create", "--email", "owner@example.com", "--password-stdin")
    code, _, err = run(_Security(), *argv, stdin=GOOD, tty=True)
    assert code == 2 and "terminal" in err
    security = _Security()
    code, _, err = run(security, *argv, stdin="short\n")
    assert code == 2 and "too short" in err
    assert security.calls == []


def test_the_prompt_asks_twice_and_again_on_a_mismatch_or_policy(
    run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = iter(["short", GOOD, "other-Passw0rd-x", GOOD, GOOD])
    monkeypatch.setattr(admin.getpass, "getpass", lambda prompt="": next(answers))
    security = _Security()
    code, _out, err = run(security, "create", "--email", "owner@example.com", tty=True)
    assert code == 0, err
    assert "too short" in err and "do not match" in err
    assert security.calls[-1][2] == GOOD


def test_invite_reissues_a_link_for_an_admin_only(run: Any) -> None:
    owner = _account(admin_role=True)
    security = _Security(owner_at_example_com=owner)
    code, out, _ = run(security, "invite", "--email", "owner@example.com")
    assert code == 0
    assert security.names() == ["reissue", "mint"]
    assert "set-password#token=" in out

    member = _Security(owner_at_example_com=_account(admin_role=False))
    code, _, err = run(member, "invite", "--email", "owner@example.com")
    assert code == 1 and "not a platform administrator" in err
    code, _, err = run(_Security(), "invite", "--email", "nobody@example.com")
    assert code == 1 and "admin create" in err


def test_an_identity_service_without_the_methods_is_named(run: Any) -> None:
    class _Old:
        async def find_user_id(self, email: str) -> None:
            return None

    code, _, err = run(_Old(), "create", "--email", "o@example.com", "--invite")
    assert code == 1
    assert "identity" in err and "upgrade" in err
    assert len(err.strip().splitlines()) == 1


def test_no_identity_module_is_named(run: Any) -> None:
    code, _, err = run(None, "create", "--email", "o@example.com", "--invite")
    assert code == 1 and "identity module" in err


async def test_grant_admin_refuses_an_unverified_account() -> None:
    squatted = _account(admin_role=False, verified=False)
    security = _Security(owner_at_example_com=squatted)
    intent = OperatorIntent(apply=True, operator="al", reason="first administrator")
    with pytest.raises(SystemExit) as info:
        await grant_admin(_Core(security), "owner@example.com", intent)
    assert "--take-over" in str(info.value.code)
    assert "appoint" not in security.names()


async def test_grant_admin_points_an_unknown_address_to_admin_create() -> None:
    intent = OperatorIntent(apply=True, operator="al", reason="first administrator")
    with pytest.raises(SystemExit) as info:
        await grant_admin(_Core(_Security()), "nobody@example.com", intent)
    assert "sandloom admin create" in str(info.value.code)
