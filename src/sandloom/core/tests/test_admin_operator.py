"""``grant-admin`` and ``sandloom admin`` across the issuer and the roles owner.

Two candidate owners from the claim map, each confirmed by describe before
the first side effect (a dry run too): the ``issuer`` finds the account by
address, the authority of ``roles`` holds the platform ``admin`` role. Here
the roles are held by a third module (acme_rbac), not by the issuer
(identity): ``grant-admin`` reads the issuer and writes acme_rbac, and
identity's roles side gets no call; ``admin list`` joins acme_rbac's holders
with identity's addresses; ``admin create`` / ``invite``, which make the
account and its role (or its admin link) in one unit, are refused before
anything is read or written (never half done). Also: mono vs split (a CLI
process with neither owner loaded) giving the same output, a missing
method, and an unassigned roles owner named by its key.
"""

from __future__ import annotations

import argparse
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

import sandloom.core.cli.admin as admin_cli
import sandloom.core.cli.security as security_cli
import sandloom.core.rpc.proxy as rpc_proxy
from sandloom.core.app_factory.claim_authorities import (
    build_claim_authorities,
    filter_to_composition,
)
from sandloom.core.claims import Authority
from sandloom.core.cli._operator import OperatorIntent
from sandloom.core.cli.operator_owner import OperatorCapabilityUnavailable
from sandloom.core.cli.security import grant_admin
from sandloom.core.config.settings import settings
from sandloom.core.contract_testing import (
    in_process_rpc_client,
    rpc_proxy_for,
    rpc_settings,
)
from sandloom.core.error_codes import code_for_exception
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import contract_method

IDENTITY = "corp.modules.identity"
RBAC = "corp.modules.acme_rbac"
IDENTITY_CONTRACT = "identity.security.v1"
RBAC_CONTRACT = "acme_rbac.security.v1"
SVC = "svc-ops"
OWNER = uuid.UUID(int=7)
OTHER = uuid.UUID(int=8)
EMAIL = "owner@example.com"
DRY = OperatorIntent(apply=False, operator="al", reason="")
APPLY = OperatorIntent(apply=True, operator="al", reason="on-call admin OPS-3")

#: Users issued by identity, roles held by acme_rbac.
SPLIT_OWNERS: dict[str, str | Authority] = {
    "issuer": Authority("identity", module=IDENTITY),
    "roles": Authority("acme_rbac", module=RBAC),
}

#: What each provider was asked, in order: ``(owner, method)``.
CALLS: list[tuple[str, str]] = []
#: The users acme_rbac made administrators.
HOLDERS: list[uuid.UUID] = []


class _IdentitySecurity:
    """The issuer, which also has a roles side (identity's own): it must
    get no roles call while acme_rbac holds the roles."""

    @classmethod
    @contract_method
    async def account_by_email(cls, email: str) -> dict[str, Any] | None:
        """The account with this address."""
        CALLS.append(("identity", "account_by_email"))
        if email != EMAIL:
            return None
        return {"user_id": str(OWNER), "email": EMAIL, "email_verified": True}

    @classmethod
    @contract_method
    async def accounts_by_id(cls, user_ids: list[uuid.UUID]) -> list[dict[str, Any]]:
        """The accounts among the ids, by address."""
        CALLS.append(("identity", "accounts_by_id"))
        known = {OWNER: EMAIL, OTHER: "second@example.com"}
        return [
            {
                "user_id": str(u),
                "email": known[u],
                "email_verified": True,
                "is_active": True,
                "password_set": u == OWNER,
                "last_login_at": None,
            }
            for u in sorted(user_ids, key=lambda u: known.get(u, ""))
            if u in known
        ]

    @classmethod
    @contract_method
    async def is_platform_admin(cls, user_id: uuid.UUID) -> bool:
        """identity's roles side: must not be asked here."""
        CALLS.append(("identity", "is_platform_admin"))
        return False

    @classmethod
    @contract_method
    async def appoint_platform_admin(
        cls, user_id: uuid.UUID, reason: str, operator: str, operation_key: str
    ) -> bool:
        """identity's roles side: must not be asked here."""
        CALLS.append(("identity", "appoint_platform_admin"))
        return True

    @classmethod
    @contract_method
    async def create_platform_admin(
        cls, email: str, full_name: str, reason: str, operator: str, key: str
    ) -> uuid.UUID:
        """Account and role in one unit: refused before this with two owners."""
        CALLS.append(("identity", "create_platform_admin"))
        return OWNER

    @classmethod
    @contract_method
    async def set_password_token(cls, user_id: uuid.UUID) -> str | None:
        """The admin's set-password link."""
        CALLS.append(("identity", "set_password_token"))
        return "tok"

    @classmethod
    @contract_method
    async def reissue_set_password(
        cls, user_id: uuid.UUID, reason: str, operator: str, key: str
    ) -> bool:
        """A new link for an administrator."""
        CALLS.append(("identity", "reissue_set_password"))
        return True


class _RbacSecurity:
    """acme_rbac, the authority of ``roles``: it holds the platform role."""

    @classmethod
    @contract_method
    async def is_platform_admin(cls, user_id: uuid.UUID) -> bool:
        """Whether the user is an administrator."""
        CALLS.append(("acme_rbac", "is_platform_admin"))
        return user_id in HOLDERS

    @classmethod
    @contract_method
    async def platform_admin_ids(cls) -> list[uuid.UUID]:
        """The administrators."""
        CALLS.append(("acme_rbac", "platform_admin_ids"))
        return [OWNER, OTHER]

    @classmethod
    @contract_method
    async def appoint_platform_admin(
        cls, user_id: uuid.UUID, reason: str, operator: str, operation_key: str
    ) -> bool:
        """Make the user an administrator."""
        CALLS.append(("acme_rbac", "appoint_platform_admin"))
        HOLDERS.append(user_id)
        return True


class _ReadOnlyRbac:
    """Roles read from LDAP: no way to grant one."""

    @classmethod
    @contract_method
    async def is_platform_admin(cls, user_id: uuid.UUID) -> bool:
        """Whether the user is an administrator."""
        CALLS.append(("acme_rbac", "is_platform_admin"))
        return False


class _FakeUoW:
    def __init__(self) -> None:
        self.session: Any = None

    async def __aenter__(self) -> _FakeUoW:
        self.session = SimpleNamespace(info={})
        return self

    async def __aexit__(self, *_: Any) -> None:
        self.session = None


class _FakeDatabase:
    def uow_factory(self) -> Any:
        return _FakeUoW

    def outbox(self) -> None:
        return None

    def audit(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _calls() -> Iterator[None]:
    CALLS.clear()
    HOLDERS.clear()
    yield
    CALLS.clear()
    HOLDERS.clear()


def _mono(
    roles: type = _RbacSecurity,
    owners: dict[str, str | Authority] | None = None,
) -> Any:
    """One process loading identity and acme_rbac."""
    core = CoreFacade(app=FastAPI())
    core.database = lambda name="default": _FakeDatabase()  # type: ignore[method-assign]
    for path, name, provider in (
        (IDENTITY, IDENTITY_CONTRACT, _IdentitySecurity),
        (RBAC, RBAC_CONTRACT, roles),
    ):
        with core.module_registration(path) as api:
            api.register_service(name, provider)
    core._claim_authorities = build_claim_authorities(
        SPLIT_OWNERS if owners is None else owners
    )
    core.module_descriptors = [
        SimpleNamespace(name=path, database_instances=lambda: ())
        for path in (IDENTITY, RBAC)
    ]
    return core


def _args(command: str, **extra: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "command": command,
        "email": EMAIL,
        "apply": True,
        "confirm_production": False,
        "reason": "on-call admin OPS-3",
        "operator": "al",
        "show_emails": False,
    }
    if command == "create":
        base.update(
            name=None,
            invite=False,
            password_stdin=True,
            if_missing=False,
            take_over=False,
            quiet=False,
        )
    base.update(extra)
    return argparse.Namespace(**base)


# --- roles held by a third module ----------------------------------------------


@pytest.mark.asyncio
async def test_grant_admin_reads_the_issuer_and_writes_the_roles_owner() -> None:
    done = await grant_admin(_mono(), EMAIL, APPLY)
    assert done == f"grant-admin: {EMAIL} ({OWNER}) is a platform administrator"
    assert CALLS == [
        ("identity", "account_by_email"),
        ("acme_rbac", "appoint_platform_admin"),
    ]
    assert HOLDERS == [OWNER]


@pytest.mark.asyncio
async def test_a_grant_dry_run_confirms_both_owners_and_writes_nothing() -> None:
    done = await grant_admin(_mono(), EMAIL, DRY)
    assert done.startswith(f"dry-run grant-admin: {EMAIL} ({OWNER}) would become")
    assert CALLS == [("identity", "account_by_email")]
    with pytest.raises(OperatorCapabilityUnavailable) as exc:
        await grant_admin(_mono(_ReadOnlyRbac), EMAIL, DRY)
    text = str(exc.value)
    assert "owner 'roles' = acme_rbac (corp.modules.acme_rbac)" in text
    assert "acme_rbac.security.v1 has no appoint_platform_admin" in text
    assert CALLS == [("identity", "account_by_email")]  # the refusal read nothing


@pytest.mark.asyncio
async def test_admin_list_joins_the_holders_with_the_issuers_addresses(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert await admin_cli.list_admins(_mono(), _args("list")) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1].split()[:2] == [EMAIL, "set"]
    assert out[2].split()[:3] == ["second@example.com", "not", "set"]
    assert CALLS == [
        ("acme_rbac", "platform_admin_ids"),
        ("identity", "accounts_by_id"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "extra"),
    [("create", {}), ("create", {"take_over": True}), ("invite", {})],
)
async def test_create_and_invite_with_two_owners_are_refused_first(
    command: str, extra: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(admin_cli, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(admin_cli, "_check_link_target", lambda: None)
    args = _args(command, **extra)
    with pytest.raises(OperatorCapabilityUnavailable) as exc:
        if command == "create":
            await admin_cli.create_admin(_mono(), args, ask=lambda _: "never")
        else:
            await admin_cli.invite_admin(_mono(), args)
    text = str(exc.value)
    assert code_for_exception(exc.value) == "SANDLOOM-B039"
    assert "owner 'issuer' = identity (corp.modules.identity)" in text
    assert "owner 'roles' = acme_rbac (corp.modules.acme_rbac)" in text
    assert "different modules" in text and "nothing was done" in text
    assert CALLS == [] and HOLDERS == []


def test_the_cli_refusal_exits_1_with_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(admin_cli, "_build_core", _mono)
    monkeypatch.setattr(admin_cli, "_stdin_is_tty", lambda: False)
    status = admin_cli.main(["create", "--email", EMAIL, "--password-stdin"])
    err = capsys.readouterr().err
    assert status == 1
    assert len(err.strip().splitlines()) == 1 and "SANDLOOM-B039" in err
    assert "grant-admin" in err
    assert CALLS == []


@pytest.mark.asyncio
async def test_an_unassigned_roles_owner_is_named_by_its_key() -> None:
    issuer_only: dict[str, str | Authority] = {
        "issuer": Authority("identity", module=IDENTITY)
    }
    with pytest.raises(OperatorCapabilityUnavailable) as exc:
        await grant_admin(_mono(owners=issuer_only), EMAIL, APPLY)
    assert "no owner is assigned for 'roles'" in str(exc.value)
    assert "platform_admin" in str(exc.value)
    assert CALLS == []


def test_grant_admin_cli_refusal_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(security_cli, "_build_core", lambda: _mono(_ReadOnlyRbac))
    with pytest.raises(SystemExit) as exc:
        security_cli.main(["grant-admin", "--email", EMAIL])
    assert "SANDLOOM-B039" in str(exc.value.code)
    assert CALLS == []


# --- mono vs split -------------------------------------------------------------


@pytest.fixture
def keyed(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    allowed = {
        SVC: {
            IDENTITY_CONTRACT: ["account_by_email", "accounts_by_id"],
            RBAC_CONTRACT: ["appoint_platform_admin", "platform_admin_ids"],
        }
    }
    for name, value in rpc_settings(SVC, allowed_calls=allowed).items():
        monkeypatch.setattr(settings, name, value)
    rpc_proxy._remote_contracts.clear()
    yield
    rpc_proxy._remote_contracts.clear()


def _cli_without_owners(
    monkeypatch: pytest.MonkeyPatch, client: Any, provider: Any
) -> Any:
    """A split's CLI process: neither owner loaded, both bound over RPC."""
    target = {"base_url": "http://sandloom-conformance", "service_id": SVC}
    pinned = {IDENTITY_CONTRACT: target, RBAC_CONTRACT: target}
    monkeypatch.setattr(settings, "service_bindings", pinned)
    return SimpleNamespace(
        claim_authorities=filter_to_composition(
            build_claim_authorities(SPLIT_OWNERS), loaded={}, bindings=pinned
        ),
        module_descriptors=[],
        service_registry=SimpleNamespace(try_get=lambda name: None),
        settings=settings,
        get_service=lambda name: rpc_proxy_for(provider, name, client),
    )


@pytest.mark.asyncio
async def test_mono_and_split_give_the_same_output(
    keyed: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mono_grant = await grant_admin(_mono(), EMAIL, APPLY)
    assert await admin_cli.list_admins(_mono(), _args("list")) == 0
    mono_list = capsys.readouterr().out
    mono_calls = list(CALLS)
    CALLS.clear()
    provider = _mono()
    async with in_process_rpc_client(provider) as client:
        cli = _cli_without_owners(monkeypatch, client, provider)
        split_grant = await grant_admin(cli, EMAIL, APPLY)
        assert await admin_cli.list_admins(cli, _args("list")) == 0
    assert split_grant == mono_grant
    assert capsys.readouterr().out == mono_list
    assert CALLS == mono_calls
    assert ("identity", "appoint_platform_admin") not in CALLS
    assert HOLDERS == [OWNER, OWNER]


@pytest.mark.asyncio
async def test_split_refuses_a_method_the_roles_owner_does_not_describe(
    keyed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _mono(_ReadOnlyRbac)
    async with in_process_rpc_client(provider) as client:
        with pytest.raises(OperatorCapabilityUnavailable) as exc:
            await grant_admin(
                _cli_without_owners(monkeypatch, client, provider), EMAIL, DRY
            )
    assert "acme_rbac.security.v1 has no appoint_platform_admin" in str(exc.value)
    assert CALLS == []
