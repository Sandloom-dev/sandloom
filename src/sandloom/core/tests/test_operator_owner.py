"""The operator owner resolver: who answers an operator command, confirmed.

``operator_owner`` reads the candidate owner of a capability from the
composition root's whole claim-authority declaration (the issuer for
sessions / accounts / credentials / the email lookup, the ``roles`` authority
for platform-admin grants, the ``tenant_ids`` authority for member
enumeration); ``confirm_operator_capability`` confirms each method by
describe, in one process and over RPC, and refuses a missing owner, method,
binding or a substituted provider before anything is done (SANDLOOM-B039 /
SANDLOOM-B040). ``<m>.security.v1`` is reserved to module ``m``
(SANDLOOM-B034).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

import sandloom.core.app_factory.claim_authorities as _claim_authorities
import sandloom.core.rpc.proxy as rpc_proxy
from sandloom.core.app_factory.claim_authorities import (
    ClaimAuthorityError,
    build_claim_authorities,
    claim_authority_problems,
    filter_to_composition,
)
from sandloom.core.claims import Authority, ClaimAuthorities
from sandloom.core.cli.consumer_copies import is_kernel_family
from sandloom.core.cli.operator_owner import (
    OperatorCapabilityUnavailable,
    OperatorOwnerSubstitution,
    confirm_operator_capability,
    forbidden_refusal,
    operator_contract,
    operator_owner,
)
from sandloom.core.config.settings import settings
from sandloom.core.contract_compat import kernel_family
from sandloom.core.contract_testing import (
    in_process_rpc_client,
    rpc_proxy_for,
    rpc_settings,
)
from sandloom.core.descriptors.module_names import InstalledModule
from sandloom.core.error_codes import code_for_exception
from sandloom.core.facade import CoreFacade
from sandloom.core.facade._services import (
    ForeignOwnerRegistration,
    reserved_contract_owner,
)
from sandloom.core.module_api import contract_method

IDP = "oidcapp.modules.acme_idp"
RBAC = "oidcapp.modules.acme_rbac"
SVC = "svc-idp"
CONTRACT = "acme_idp.security.v1"

#: The third-party OIDC root (examples/oidc-identity): acme_idp issues and
#: holds the rights, tenancy and billing keep their keys.
OIDC_MAP: dict[str, str | Authority] = {
    "issuer": Authority("acme_idp", module=IDP),
    "roles": Authority("acme_idp", module=IDP),
    "permissions": Authority("acme_idp", module=IDP),
    "tenant_ids": Authority("tenancy", module="sandloom_tenancy"),
}


class _AcmeSecurity:
    """An external-IdP operator contract: sessions and accounts, no passwords."""

    @classmethod
    @contract_method
    async def list_user_ids(cls, after: str | None, limit: int) -> list[str]:
        """No user is listed here."""
        return []

    @classmethod
    @contract_method
    async def end_sessions(cls, user_ids: list[str], reason: str) -> int:
        """Every listed user's sessions end."""
        return len(user_ids)


def _core(authorities: ClaimAuthorities, **extra: Any) -> Any:
    """A composition core as the resolver reads it (no provider here)."""
    fields: dict[str, Any] = {
        "claim_authorities": authorities,
        "module_descriptors": [],
        "service_registry": SimpleNamespace(try_get=lambda name: None),
        "settings": SimpleNamespace(service_bindings={}),
    }
    fields.update(extra)
    return SimpleNamespace(**fields)


# --- resolver --------------------------------------------------------------


def test_third_party_oidc_issuer_owns_sessions_and_roles() -> None:
    core = _core(build_claim_authorities(OIDC_MAP))
    for capability in ("sessions", "credentials", "user_lookup", "platform_admin"):
        owner = operator_owner(core, capability, command="x")
        assert (owner.module, owner.path, owner.contract) == ("acme_idp", IDP, CONTRACT)
    assert operator_owner(core, "tenant_members", command="x").module == "tenancy"


def test_different_owners_for_issuer_roles_and_tenant_ids() -> None:
    authorities = build_claim_authorities(
        {
            "issuer": Authority("acme_idp", module=IDP),
            "roles": Authority("acme_rbac", module=RBAC),
            "tenant_ids": Authority("tenancy", module="sandloom_tenancy"),
        }
    )
    core = _core(authorities)
    assert operator_owner(core, "accounts", command="x").contract == CONTRACT
    assert (
        operator_owner(core, "platform_admin", command="x").contract
        == "acme_rbac.security.v1"
    )
    assert (
        operator_owner(core, "tenant_members", command="x").contract
        == "tenancy.security.v1"
    )


def test_split_process_without_the_issuer_keeps_the_whole_declaration() -> None:
    """``filter_to_composition`` clears the minting issuer, never the declared one."""
    whole = build_claim_authorities(OIDC_MAP)
    filtered = filter_to_composition(
        whole,
        loaded={"tenancy": "sandloom_tenancy"},
        bindings={"acme_idp.claims.v1": {"service_id": SVC}},
    )
    assert filtered.issuer is None
    assert filtered.declared_issuer == IDP
    assert filtered.names[IDP] == "acme_idp"
    core = _core(filtered)
    assert operator_owner(core, "sessions", command="x").contract == CONTRACT
    assert operator_owner(core, "platform_admin", command="x").contract == CONTRACT


def test_a_path_only_owner_loaded_here_takes_the_loaders_name() -> None:
    authorities = build_claim_authorities({"issuer": IDP})
    core = _core(authorities, module_descriptors=[SimpleNamespace(name=IDP)])
    assert operator_owner(core, "sessions", command="x").module == "acme_idp"


def test_a_path_only_owner_not_here_has_no_guessed_name() -> None:
    """The last import segment is never taken as the name (decision 5)."""
    core = _core(build_claim_authorities({"issuer": IDP}))
    with pytest.raises(OperatorCapabilityUnavailable) as exc:
        operator_owner(core, "sessions", command="revoke-sessions")
    text = str(exc.value)
    assert "SANDLOOM-B039" in text and "revoke-sessions" in text
    assert f"Authority('<name>', module={IDP!r})" in text
    assert exc.value.code == text  # exit status 1, the message on stderr


def test_no_map_and_unassigned_key_are_refused_naming_the_key() -> None:
    with pytest.raises(OperatorCapabilityUnavailable, match="no claim_authorities"):
        operator_owner(_core(ClaimAuthorities()), "sessions", command="x")
    core = _core(build_claim_authorities({"issuer": Authority("acme_idp", module=IDP)}))
    with pytest.raises(OperatorCapabilityUnavailable) as exc:
        operator_owner(core, "tenant_members", command="revoke-sessions")
    assert "no owner is assigned for 'tenant_ids'" in str(exc.value)
    assert code_for_exception(exc.value) == "SANDLOOM-B039"


def test_a_declared_name_must_match_the_installed_or_loaded_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed = InstalledModule("acme", "acme_dist", "acme-dist")
    monkeypatch.setattr(_claim_authorities, "installed_modules", lambda: (installed,))
    with pytest.raises(ClaimAuthorityError, match="installed as 'acme'"):
        build_claim_authorities({"issuer": Authority("idp", module="acme_dist")})
    assert build_claim_authorities({"issuer": "acme_dist"}).names == {
        "acme_dist": "acme"
    }
    monkeypatch.undo()
    with pytest.raises(ClaimAuthorityError, match="public"):
        build_claim_authorities({"issuer": Authority("a.b", module=IDP)})
    authorities = build_claim_authorities({"issuer": Authority("idp", module=IDP)})
    problems = claim_authority_problems(
        authorities,
        loaded={"acme_idp": IDP},
        contributors={},
        bindings={},
        issuers=[],
    )
    assert any("loaded here as 'acme_idp'" in p for p in problems)


def test_the_verifier_owner_uses_the_declared_name() -> None:
    authorities = build_claim_authorities(OIDC_MAP)
    assert authorities.verified_owners is not None
    assert authorities.verified_owners["roles"] == "acme_idp"


# --- reservation (B034) ------------------------------------------------------


def test_security_contract_has_no_kernel_shape() -> None:
    """Reserved by name only: copied, stubbed and compared like any contract."""
    assert kernel_family(CONTRACT) is None
    assert not is_kernel_family(CONTRACT)


def test_security_contract_is_reserved_to_its_module() -> None:
    assert reserved_contract_owner(CONTRACT) == "acme_idp"
    core = CoreFacade(app=FastAPI())
    with (
        pytest.raises(ForeignOwnerRegistration, match="SANDLOOM-B034"),
        core.module_registration("oidcapp.modules.squatter") as api,
    ):
        api.register_service(CONTRACT, _AcmeSecurity)
    assert not core.service_registry.contains(CONTRACT)
    with core.module_registration(IDP) as api:
        api.register_service(CONTRACT, _AcmeSecurity)
    assert core.contract_owner(CONTRACT) == IDP


# --- confirmation in one process ---------------------------------------------


def _local(registrant: str) -> CoreFacade:
    core = CoreFacade(app=FastAPI())
    with core.module_registration(registrant) as api:
        api.register_service(CONTRACT, _AcmeSecurity)
    core._claim_authorities = build_claim_authorities(OIDC_MAP)
    return core


@pytest.mark.asyncio
async def test_mono_confirms_present_methods() -> None:
    core = _local(IDP)
    owner, service = await operator_contract(
        core, "sessions", ["end_sessions", "list_user_ids"], command="revoke-sessions"
    )
    assert owner.contract == CONTRACT
    assert "end_sessions" in service.contract_schema.methods


@pytest.mark.asyncio
async def test_mono_missing_method_is_a_named_refusal() -> None:
    core = _local(IDP)
    with pytest.raises(OperatorCapabilityUnavailable) as exc:
        await operator_contract(
            core,
            "credentials",
            ["require_password_reset"],
            command="force-password-reset",
        )
    assert (
        f"force-password-reset: owner 'issuer' = acme_idp ({IDP}); "
        "acme_idp.security.v1 has no require_password_reset. Do it at the "
        "external provider, or add the method to acme_idp."
    ) in str(exc.value)


@pytest.mark.asyncio
async def test_mono_registrant_of_another_path_is_a_substitution() -> None:
    core = _local("otherapp.modules.acme_idp")
    with pytest.raises(OperatorOwnerSubstitution) as exc:
        await operator_contract(core, "sessions", ["end_sessions"], command="x")
    assert "otherapp.modules.acme_idp" in str(exc.value)
    assert code_for_exception(exc.value) == "SANDLOOM-B040"


@pytest.mark.asyncio
async def test_owner_loaded_without_its_contract_is_refused() -> None:
    core = CoreFacade(app=FastAPI())
    core._claim_authorities = build_claim_authorities(OIDC_MAP)
    core.module_descriptors = [SimpleNamespace(name=IDP)]
    with pytest.raises(OperatorCapabilityUnavailable, match="registers no"):
        await operator_contract(core, "sessions", ["end_sessions"], command="x")


# --- confirmation over RPC ---------------------------------------------------


@pytest.fixture
def keyed(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    allowed = {SVC: {CONTRACT: ["end_sessions", "list_user_ids"]}}
    for name, value in rpc_settings(SVC, allowed_calls=allowed).items():
        monkeypatch.setattr(settings, name, value)
    rpc_proxy._remote_contracts.clear()
    yield


def _bound(
    monkeypatch: pytest.MonkeyPatch, client: Any, provider: CoreFacade, **bindings: Any
) -> Any:
    """A caller process with no provider, binding ``CONTRACT`` to ``SVC``."""
    pinned = {CONTRACT: {"base_url": "http://sandloom-conformance", "service_id": SVC}}
    pinned.update(bindings)
    monkeypatch.setattr(settings, "service_bindings", pinned)
    filtered = filter_to_composition(
        build_claim_authorities(OIDC_MAP), loaded={}, bindings=pinned
    )
    return _core(
        filtered,
        settings=settings,
        get_service=lambda name: rpc_proxy_for(provider, name, client),
    )


@pytest.mark.asyncio
async def test_split_confirms_by_live_describe(
    keyed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _local(IDP)
    async with in_process_rpc_client(provider) as client:
        caller = _bound(
            monkeypatch,
            client,
            provider,
            **{"acme_idp.claims.v1": {"base_url": "x", "service_id": SVC}},
        )
        owner, service = await operator_contract(
            caller, "sessions", ["end_sessions"], command="revoke-sessions"
        )
        assert isinstance(service, rpc_proxy.RemoteServiceProxy)
        assert owner.module == "acme_idp"
        with pytest.raises(OperatorCapabilityUnavailable) as exc:
            await confirm_operator_capability(caller, owner, ["list_user_ids", "nope"])
    # Not granted to this service: named before any call.
    assert "may not call acme_idp.security.v1.nope" in str(exc.value)
    assert "RPC_ALLOWED_CALLS" in str(exc.value)


@pytest.mark.asyncio
async def test_split_missing_method_is_refused(
    keyed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed = {SVC: [CONTRACT]}
    monkeypatch.setattr(settings, "rpc_allowed_calls", allowed)
    provider = _local(IDP)
    async with in_process_rpc_client(provider) as client:
        caller = _bound(monkeypatch, client, provider)
        with pytest.raises(OperatorCapabilityUnavailable) as exc:
            await operator_contract(
                caller, "credentials", ["require_password_reset"], command="fpr"
            )
    assert "acme_idp.security.v1 has no require_password_reset" in str(exc.value)


@pytest.mark.asyncio
async def test_split_binding_naming_another_service_is_refused(
    keyed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _local(IDP)
    async with in_process_rpc_client(provider) as client:
        caller = _bound(
            monkeypatch,
            client,
            provider,
            **{"acme_idp.claims.v1": {"base_url": "x", "service_id": "svc-real-idp"}},
        )
        with pytest.raises(OperatorOwnerSubstitution) as exc:
            await operator_contract(caller, "sessions", ["end_sessions"], command="x")
    assert "acme_idp.claims.v1 -> 'svc-real-idp'" in str(exc.value)


@pytest.mark.asyncio
async def test_split_provider_answering_for_another_module_is_refused(
    keyed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _local("otherapp.modules.acme_idp")
    async with in_process_rpc_client(provider) as client:
        caller = _bound(monkeypatch, client, provider)
        with pytest.raises(OperatorOwnerSubstitution) as exc:
            await operator_contract(caller, "sessions", ["end_sessions"], command="x")
    assert "'otherapp.modules.acme_idp'" in str(exc.value)


@pytest.mark.asyncio
async def test_split_provider_403_is_a_missing_grant(
    keyed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _local(IDP)
    strict = settings.model_copy(
        update=rpc_settings(SVC, allowed_calls={SVC: ["other.thing.v1"]})
    )
    async with in_process_rpc_client(provider, settings=strict) as client:
        caller = _bound(monkeypatch, client, provider)
        with pytest.raises(OperatorCapabilityUnavailable) as exc:
            await operator_contract(caller, "sessions", ["end_sessions"], command="x")
    assert "(403)" in str(exc.value) and "'end_sessions'" in str(exc.value)
    owner = operator_owner(caller, "sessions", command="revoke-sessions")
    refusal = forbidden_refusal(caller, owner, "end_sessions")
    assert "RPC_ALLOWED_CALLS" in str(refusal) and "end_sessions" in str(refusal)


def test_split_unbound_contract_is_refused() -> None:
    core = _core(build_claim_authorities(OIDC_MAP))
    owner = operator_owner(core, "sessions", command="x")
    with pytest.raises(OperatorCapabilityUnavailable, match="neither registered"):
        asyncio.run(confirm_operator_capability(core, owner, ["end_sessions"]))
