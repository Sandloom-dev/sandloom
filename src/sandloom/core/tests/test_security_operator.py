"""``sandloom security`` session commands on the operator resolver (B-R5).

Who answers is the claim map's candidate owner, confirmed by describe before
the first side effect, a dry run included: the issuer for ``end_sessions`` /
``require_password_reset`` / ``list_user_ids``, the ``tenant_ids`` owner for
``list_member_user_ids``. The kernel revokes the access tokens itself (any
issuer); the report says, kind by kind, what was done. Here: a third-party
stateless OIDC issuer (acme_idp), ``tenant_ids`` held by another module,
missing methods, a missing grant, and mono vs split (a CLI process without
the issuer) giving the same report. The kernel's real unit of work (audit,
mark, durable outbox record, every service's applier):
``tests/integration/test_security_revocation_split.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from fastapi import FastAPI
from pydantic import BaseModel

import sandloom.core.cli.security as security_cli
import sandloom.core.rpc.proxy as rpc_proxy
from sandloom.core.app_factory.claim_authorities import (
    build_claim_authorities,
    filter_to_composition,
)
from sandloom.core.claims import Authority
from sandloom.core.cli._operator import OperatorIntent
from sandloom.core.cli.operator_owner import OperatorCapabilityUnavailable
from sandloom.core.cli.security import (
    BulkResult,
    Targets,
    run_bulk,
    summary_line,
)
from sandloom.core.config.settings import settings
from sandloom.core.contract_testing import (
    in_process_rpc_client,
    rpc_proxy_for,
    rpc_settings,
)
from sandloom.core.error_codes import code_for_exception
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import contract_method

IDP = "oidcapp.modules.acme_idp"
ORGS = "oidcapp.modules.acme_orgs"
SVC = "svc-idp"
CONTRACT = "acme_idp.security.v1"
USERS = sorted(uuid.uuid4() for _ in range(3))
DRY = OperatorIntent(apply=False, operator="al", reason="")
APPLY = OperatorIntent(apply=True, operator="al", reason="token leak SEC-7")

#: The third-party OIDC root (examples/oidc-identity), ``tenant_ids`` held by
#: another app module instead of tenancy.
OIDC_MAP: dict[str, str | Authority] = {
    "issuer": Authority("acme_idp", module=IDP),
    "roles": Authority("acme_idp", module=IDP),
    "tenant_ids": Authority("acme_orgs", module=ORGS),
}

#: What the provider of each contract was asked, in order.
CALLS: list[tuple[str, Any]] = []


class _AcmeSecurity:
    """A stateless OIDC issuer's operator contract: no passwords."""

    @classmethod
    @contract_method
    async def list_user_ids(
        cls, after: uuid.UUID | None, limit: int
    ) -> list[uuid.UUID]:
        """Every account, in id order."""
        rest = [u for u in USERS if after is None or u > after]
        return rest[:limit]

    @classmethod
    @contract_method
    async def end_sessions(
        cls, user_ids: list[uuid.UUID], reason: str, operator: str, operation_key: str
    ) -> SessionsEnded:
        """Its sessions are the access tokens it minted (the kernel revokes
        those itself)."""
        CALLS.append(("end_sessions", [str(u) for u in user_ids]))
        return SessionsEnded(affected=len(user_ids), ended=["access_tokens"])


class SessionsEnded(BaseModel):
    """The issuer's ``end_sessions`` answer (the shape the kernel reads)."""

    affected: int
    ended: list[Literal["access_tokens", "refresh_sessions", "provider_sessions"]]


class _AccessOnly(BaseModel):
    """An ``end_sessions`` answer that can never report refresh sessions."""

    affected: int
    ended: list[Literal["access_tokens"]]


class _NoRefreshSecurity:
    """An issuer whose ``end_sessions`` cannot say refresh sessions ended."""

    @classmethod
    @contract_method
    async def end_sessions(
        cls, user_ids: list[uuid.UUID], reason: str, operator: str, operation_key: str
    ) -> _AccessOnly:
        """Never reached: refused before the first side effect."""
        CALLS.append(("end_sessions", [str(u) for u in user_ids]))
        return _AccessOnly(affected=len(user_ids), ended=["access_tokens"])


class _OrgsSecurity:
    """``tenant_ids`` held by acme_orgs: it lists an organization's members."""

    @classmethod
    @contract_method
    async def list_member_user_ids(
        cls, organization_id: uuid.UUID, after: uuid.UUID | None, limit: int
    ) -> list[uuid.UUID]:
        """The first two users are members."""
        CALLS.append(("list_member_user_ids", str(organization_id)))
        rest = [u for u in USERS[:2] if after is None or u > after]
        return rest[:limit]


class _FakeUoW:
    """A provider read unit without a database (the reads write nothing)."""

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


class _Revoked:
    """The kernel's part, recorded."""

    def __init__(self) -> None:
        self.batches: list[list[uuid.UUID]] = []

    async def __call__(
        self, ids: Any, *, command: str, intent: OperatorIntent, key: str
    ) -> bool:
        self.batches.append(list(ids))
        return True


@pytest.fixture(autouse=True)
def _calls() -> Iterator[None]:
    CALLS.clear()
    yield
    CALLS.clear()


def _mono(*registrations: tuple[str, str, type], stateless: bool = True) -> Any:
    """One process loading acme_idp (and what else is registered)."""
    core = CoreFacade(app=FastAPI())
    core.database = lambda name="default": _FakeDatabase()  # type: ignore[method-assign]
    for path, name, provider in registrations:
        with core.module_registration(path) as api:
            api.register_service(name, provider)
    core._claim_authorities = build_claim_authorities(OIDC_MAP)
    core.module_descriptors = [
        SimpleNamespace(
            name=IDP,
            issues_tokens=True,
            refresh_sessions=not stateless,
            database_instances=lambda: (),
        )
    ]
    return core


def _report(result: BulkResult) -> list[str]:
    return [summary_line(result), *result.lines()]


# --- a third-party stateless issuer ------------------------------------------


@pytest.mark.asyncio
async def test_stateless_oidc_issuer_report_says_what_was_done() -> None:
    revoked = _Revoked()
    result = await run_bulk(
        _mono((IDP, CONTRACT, _AcmeSecurity)),
        "revoke-sessions",
        Targets(all_users=True),
        APPLY,
        operation_id="op-1",
        revoke=revoked,
    )
    assert revoked.batches == [USERS]
    assert CALLS == [("end_sessions", [str(u) for u in USERS])]
    text = "\n".join(_report(result))
    for line in (
        "access tokens revoked",
        "refresh sessions not applicable: acme_idp holds none",
        "external IdP sessions not controlled",
        "new sign-in still possible: revocation is not a block",
    ):
        assert line in text
    assert result.as_dict()["sessions"] == {
        "access_tokens": "revoked",
        "refresh_sessions": "not_applicable",
        "provider_sessions": "not_controlled",
        "sign_in": "still_possible",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", [DRY, APPLY], ids=["dry-run", "apply"])
async def test_force_password_reset_without_passwords_is_refused_first(
    intent: OperatorIntent,
) -> None:
    revoked = _Revoked()
    with pytest.raises(OperatorCapabilityUnavailable) as exc:
        await run_bulk(
            _mono((IDP, CONTRACT, _AcmeSecurity)),
            "force-password-reset",
            Targets(users=tuple(USERS)),
            intent,
            operation_id="op-2",
            revoke=revoked,
        )
    assert code_for_exception(exc.value) == "SANDLOOM-B039"
    assert (
        f"force-password-reset: owner 'issuer' = acme_idp ({IDP}); "
        "acme_idp.security.v1 has no require_password_reset. Do it at the "
        "external provider"
    ) in str(exc.value)
    # Nothing was done: no issuer call, no kernel unit (no audit row, no mark).
    assert CALLS == []
    assert revoked.batches == []


@pytest.mark.asyncio
async def test_dry_run_with_listed_users_confirms_the_issuer() -> None:
    """A dry run with ``--user`` never enumerates, but still confirms."""
    with pytest.raises(OperatorCapabilityUnavailable, match="registers no"):
        await run_bulk(
            _mono(),
            "revoke-sessions",
            Targets(users=(USERS[0],)),
            DRY,
            operation_id="op-3",
        )


@pytest.mark.asyncio
async def test_a_refresh_holding_issuer_must_report_its_refresh_sessions() -> None:
    """``refresh_sessions=True`` declared here, not ended: exit 1, after the
    kernel revoked the batch's access tokens (the mandatory part)."""
    revoked = _Revoked()
    with pytest.raises(SystemExit) as exc:
        await run_bulk(
            _mono((IDP, CONTRACT, _AcmeSecurity), stateless=False),
            "revoke-sessions",
            Targets(users=tuple(USERS)),
            APPLY,
            operation_id="op-4",
            revoke=revoked,
        )
    assert "declares refresh_sessions=True" in str(exc.value.code)
    assert "--operation-id op-4" in str(exc.value.code)
    assert revoked.batches == [USERS]


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", [DRY, APPLY], ids=["dry-run", "apply"])
async def test_an_issuer_that_cannot_report_refresh_sessions_is_refused_first(
    intent: OperatorIntent,
) -> None:
    """Declared ``refresh_sessions=True``, but its ``end_sessions`` answer
    type cannot say they ended: refused before anything, a dry run too."""
    revoked = _Revoked()
    with pytest.raises(OperatorCapabilityUnavailable) as exc:
        await run_bulk(
            _mono((IDP, CONTRACT, _NoRefreshSecurity), stateless=False),
            "revoke-sessions",
            Targets(users=tuple(USERS)),
            intent,
            operation_id="op-4b",
            revoke=revoked,
        )
    assert "cannot report 'refresh_sessions'" in str(exc.value)
    assert CALLS == []
    assert revoked.batches == []


class _DtoResetSecurity(_AcmeSecurity):
    """An issuer whose ``require_password_reset`` answers a DTO, not a count."""

    @classmethod
    @contract_method
    async def require_password_reset(
        cls, user_ids: list[uuid.UUID], reason: str, operator: str, operation_key: str
    ) -> SessionsEnded:
        """Never reached: refused before the first side effect."""
        CALLS.append(("require_password_reset", [str(u) for u in user_ids]))
        return SessionsEnded(affected=0, ended=[])


@pytest.mark.asyncio
@pytest.mark.parametrize("split", [False, True], ids=["mono", "split"])
@pytest.mark.parametrize("intent", [DRY, APPLY], ids=["dry-run", "apply"])
async def test_an_incompatible_method_is_refused_before_anything(
    intent: OperatorIntent, split: bool, keyed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The schema of ``require_password_reset`` (local, or the live describe
    in a split) is not the shape the kernel calls: B039, expected against
    got, before any call, a dry run too."""
    allowed = {SVC: {CONTRACT: ["end_sessions", "require_password_reset"]}}
    for name, value in rpc_settings(SVC, allowed_calls=allowed).items():
        monkeypatch.setattr(settings, name, value)
    provider = _mono((IDP, CONTRACT, _DtoResetSecurity), stateless=False)
    revoked = _Revoked()
    async with in_process_rpc_client(provider) as client:
        core = _cli_without_issuer(monkeypatch, client, provider) if split else provider
        with pytest.raises(OperatorCapabilityUnavailable) as exc:
            await run_bulk(
                core,
                "force-password-reset",
                Targets(users=tuple(USERS)),
                intent,
                operation_id="op-9",
                revoke=revoked,
            )
    text = str(exc.value)
    assert "SANDLOOM-B039" in text
    assert "incompatible require_password_reset: expected (" in text
    assert ") -> int, got (" in text
    assert "Nothing was done" in text
    assert CALLS == []
    assert revoked.batches == []


@pytest.mark.asyncio
async def test_split_holds_the_issuer_to_its_refresh_sessions_declaration(
    keyed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CLI process without the issuer reads ``refresh_sessions=True`` from
    the issuer's describe (it has no descriptor of it): the batch's access
    tokens are revoked, and the missing refresh-session end is a failure,
    as in one process."""
    provider = _mono((IDP, CONTRACT, _AcmeSecurity), stateless=False)
    revoked = _Revoked()
    async with in_process_rpc_client(provider) as client:
        with pytest.raises(SystemExit) as exc:
            await run_bulk(
                _cli_without_issuer(monkeypatch, client, provider),
                "revoke-sessions",
                Targets(users=tuple(USERS)),
                APPLY,
                operation_id="op-4c",
                revoke=revoked,
            )
    assert "declares refresh_sessions=True" in str(exc.value.code)
    assert revoked.batches == [USERS]


# --- tenant_ids owned by another module ---------------------------------------


@pytest.mark.asyncio
async def test_tenant_members_come_from_the_tenant_ids_owner() -> None:
    org = uuid.uuid4()
    revoked = _Revoked()
    result = await run_bulk(
        _mono(
            (IDP, CONTRACT, _AcmeSecurity),
            (ORGS, "acme_orgs.security.v1", _OrgsSecurity),
        ),
        "revoke-sessions",
        Targets(tenant=org),
        APPLY,
        operation_id="op-5",
        revoke=revoked,
    )
    assert CALLS[0] == ("list_member_user_ids", str(org))
    assert revoked.batches == [USERS[:2]]
    assert result.users == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", [DRY, APPLY], ids=["dry-run", "apply"])
async def test_a_tenant_ids_owner_without_the_method_is_refused(
    intent: OperatorIntent,
) -> None:
    revoked = _Revoked()
    with pytest.raises(OperatorCapabilityUnavailable) as exc:
        await run_bulk(
            _mono(
                (IDP, CONTRACT, _AcmeSecurity),
                (ORGS, "acme_orgs.security.v1", _AcmeSecurity),
            ),
            "revoke-sessions",
            Targets(tenant=uuid.uuid4()),
            intent,
            operation_id="op-6",
            revoke=revoked,
        )
    assert "owner 'tenant_ids' = acme_orgs" in str(exc.value)
    assert "acme_orgs.security.v1 has no list_member_user_ids" in str(exc.value)
    assert (CALLS, revoked.batches) == ([], [])


# --- mono vs split -------------------------------------------------------------


@pytest.fixture
def keyed(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    allowed = {SVC: {CONTRACT: ["end_sessions", "list_user_ids"]}}
    for name, value in rpc_settings(SVC, allowed_calls=allowed).items():
        monkeypatch.setattr(settings, name, value)
    rpc_proxy._remote_contracts.clear()
    yield
    rpc_proxy._remote_contracts.clear()


def _cli_without_issuer(
    monkeypatch: pytest.MonkeyPatch, client: Any, provider: Any
) -> Any:
    """A split's CLI process: no module loaded, the issuer bound over RPC."""
    pinned = {
        CONTRACT: {"base_url": "http://sandloom-conformance", "service_id": SVC},
        "acme_idp.claims.v1": {"base_url": "x", "service_id": SVC},
    }
    monkeypatch.setattr(settings, "service_bindings", pinned)
    return SimpleNamespace(
        claim_authorities=filter_to_composition(
            build_claim_authorities(OIDC_MAP), loaded={}, bindings=pinned
        ),
        module_descriptors=[],
        service_registry=SimpleNamespace(try_get=lambda name: None),
        settings=settings,
        get_service=lambda name: rpc_proxy_for(provider, name, client),
    )


@pytest.mark.asyncio
async def test_mono_and_split_give_the_same_report(
    keyed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    mono = await run_bulk(
        _mono((IDP, CONTRACT, _AcmeSecurity)),
        "revoke-sessions",
        Targets(all_users=True),
        APPLY,
        operation_id="same",
        revoke=_Revoked(),
    )
    provider = _mono((IDP, CONTRACT, _AcmeSecurity))
    revoked = _Revoked()
    async with in_process_rpc_client(provider) as client:
        split = await run_bulk(
            _cli_without_issuer(monkeypatch, client, provider),
            "revoke-sessions",
            Targets(all_users=True),
            APPLY,
            operation_id="same",
            revoke=revoked,
        )
    assert revoked.batches == [USERS]
    assert _report(split) == _report(mono)
    assert split.as_dict() == mono.as_dict()


@pytest.mark.asyncio
async def test_split_call_refused_by_the_provider_names_the_missing_grant(
    keyed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller's grant lists end_sessions, the provider's does not: the 403
    of the call is the missing ``RPC_ALLOWED_CALLS`` grant, named."""
    provider = _mono((IDP, CONTRACT, _AcmeSecurity))
    strict = settings.model_copy(
        update=rpc_settings(SVC, allowed_calls={SVC: {CONTRACT: ["list_user_ids"]}})
    )
    revoked = _Revoked()
    async with in_process_rpc_client(provider, settings=strict) as client:
        with pytest.raises(OperatorCapabilityUnavailable) as exc:
            await run_bulk(
                _cli_without_issuer(monkeypatch, client, provider),
                "revoke-sessions",
                Targets(users=(USERS[0],)),
                APPLY,
                operation_id="op-7",
                revoke=revoked,
            )
    text = str(exc.value)
    assert "SANDLOOM-B039" in text and "RPC_ALLOWED_CALLS" in text
    assert "end_sessions" in text
    assert revoked.batches == []


def test_cli_prints_the_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    core = _mono((IDP, CONTRACT, _AcmeSecurity))
    monkeypatch.setattr(security_cli, "_build_core", lambda: core)
    monkeypatch.setattr(security_cli, "revoke_access_tokens", _Revoked())
    status = security_cli.main(
        [
            "revoke-sessions",
            "--user",
            str(USERS[0]),
            "--apply",
            "--reason",
            "token leak SEC-8",
            "--operator",
            "al",
        ]
    )
    out = capsys.readouterr().out
    assert status == 0
    assert out.splitlines()[0].startswith(
        "revoke-sessions: 1 user(s) affected of 1 targeted (1 listed user(s))"
    )
    assert "issuer acme_idp" in out
    assert "refresh sessions not applicable" in out
