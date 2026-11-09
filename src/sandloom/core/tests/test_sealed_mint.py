"""The sealed mint, service tokens and test tokens (ADR-0014 decision 5,
owner answer #13, adversarial C-a2; SANDLOOM-R017).

* T-11: code in the issuer process that hands ``create_access_token``
  protected claims, provenance or ``cpv`` outside the sealed
  ``collect_claims`` result is refused; a service token
  (``create_service_token``) authenticates and every gate answers 403.
* ``collect_claims`` seals its inputs: the issuer is resolved from the
  module that registered the contributor it is identified by (never a
  string), ``precomputed`` is the verified issuer's own and carries only
  keys the map assigns to it.
* T-12: ``as_user`` / ``mint_test_token`` grant rights through the kernel
  test authority in a test composition and are refused outside one; a test
  that states ``claims_sources`` gets that provenance checked.

Kernel-only: fake module paths on a facade with fake databases (no I/O
beyond the in-memory revocation cache).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI

from sandloom.core import error_codes
from sandloom.core.app_factory.claim_authorities import build_claim_authorities
from sandloom.core.auth import claims_view as view
from sandloom.core.auth import tokens
from sandloom.core.claims import (
    AUTHORITY_CLAIM,
    CPV_VERSION,
    SealedClaims,
    UnsealedClaimsError,
)
from sandloom.core.facade import ClaimsUnavailable, CoreFacade
from sandloom.core.runtime import use_active_core
from sandloom.core.testing.auth import make_user_headers, mint_test_token, sealed_claims
from sandloom.core.tests import test_auth_rotation_revocation as _revocation
from sandloom.core.tests.test_claims_split_matrix import GATES, statuses
from sandloom.core.tests.test_claims_trust import _FakeDatabase, _register

mem_cache = _revocation.mem_cache

IDP = "acme.idp"
GRANTS = "acme.grants"
ROGUE = "acme.rogue"
MAP: dict[str, Any] = {
    "issuer": IDP,
    "roles": IDP,
    "permissions": IDP,
    "tenant_ids": GRANTS,
    "loyalty_tier": IDP,
}
TENANT = str(uuid4())
ENVELOPE = {"token_type": "access"}
ALL_403 = dict.fromkeys(GATES, 403)
ALL_200 = dict.fromkeys(GATES, 200)

#: Every key a mint takes only from a sealed result: the protected claims
#: (base and an application key the map assigns), the tenant selectors,
#: provenance, ``cpv``, the subject structure and the authority binding.
SEALED_ONLY = (
    "roles",
    "permissions",
    "tenant_ids",
    "email",
    "email_verified",
    "subscription_status",
    "plan_code",
    "features",
    "limits",
    "loyalty_tier",
    "tenant_id",
    "org_id",
    "claims_sources",
    "claims_sources_failed",
    "cpv",
    "user_id",
    "provenance_authoritative",
    "claims",
    AUTHORITY_CLAIM,
)


def _core() -> CoreFacade:
    core = CoreFacade(app=FastAPI())
    core.database = lambda name="default": _FakeDatabase()  # type: ignore[method-assign]
    core._assign_claim_authorities(build_claim_authorities(MAP))
    return core


@pytest.fixture
def core() -> CoreFacade:
    """The issuer and verifier: ``acme.idp`` vouches for ``roles`` /
    ``permissions``, ``acme.grants`` for the tenant grant."""
    core = _core()
    _register(core, IDP, {"roles": ["admin"], "permissions": ["x.read"]}, critical=True)
    _register(core, GRANTS, {"tenant_ids": [TENANT]}, critical=True)
    return core


@pytest.fixture(autouse=True)
def _fresh_verification_cache() -> Iterator[None]:
    tokens._verified_tokens.clear()
    yield
    tokens._verified_tokens.clear()


def _envelope() -> dict[str, Any]:
    return {"sub": str(uuid4()), **ENVELOPE}


# --------------------------------------------------------------------------- #
# the sealed result
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_collect_claims_returns_a_sealed_read_only_result(
    core: CoreFacade,
) -> None:
    sealed = await core.collect_claims(uuid4())
    assert isinstance(sealed, SealedClaims) and sealed.is_sealed()
    assert sealed["roles"] == ["admin"]
    assert sealed["claims_sources"] == ["grants", "idp"]
    with pytest.raises(TypeError):
        sealed["roles"] = ["owner"]  # type: ignore[index]
    with pytest.raises(AttributeError):
        sealed._data = {}  # type: ignore[misc]
    with pytest.raises(UnsealedClaimsError, match="SANDLOOM-R017"):
        SealedClaims({"roles": ["admin"]})


@pytest.mark.asyncio
@pytest.mark.usefixtures("mem_cache")
async def test_the_sealed_result_mints_a_token_every_gate_trusts(
    core: CoreFacade,
) -> None:
    sealed = await core.collect_claims(uuid4())
    token = tokens.create_access_token(_envelope(), claims=sealed)
    assert (await statuses(core, token))["/role"] == 200
    with use_active_core(core):
        payload = tokens.decode_token(token)
    assert payload is not None
    assert view.UNTRUSTED_KEY not in payload
    assert payload["tenant_ids"] == [TENANT]


# --------------------------------------------------------------------------- #
# T-11: hand-minting is refused
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", SEALED_ONLY)
def test_a_hand_minted_sealed_key_is_refused(core: CoreFacade, key: str) -> None:
    with use_active_core(core), pytest.raises(UnsealedClaimsError) as refused:
        tokens.create_access_token({**_envelope(), key: ["admin"]})
    assert "SANDLOOM-R017" in str(refused.value)
    assert key in str(refused.value)
    assert error_codes.code_for_exception(refused.value) == "SANDLOOM-R017"


def test_without_an_active_composition_the_base_keys_are_still_refused() -> None:
    """No core bound: the protected base, provenance and ``cpv`` still are."""
    for key in ("roles", "tenant_ids", "email", "claims_sources", "cpv"):
        with pytest.raises(UnsealedClaimsError):
            tokens.create_access_token({**_envelope(), key: "x"})


@pytest.mark.asyncio
async def test_a_spread_sealed_result_is_a_plain_dict_and_refused(
    core: CoreFacade,
) -> None:
    sealed = await core.collect_claims(uuid4())
    with use_active_core(core), pytest.raises(UnsealedClaimsError):
        tokens.create_access_token({**sealed, **_envelope()})
    with use_active_core(core), pytest.raises(UnsealedClaimsError, match="sealed"):
        tokens.create_access_token(_envelope(), claims=dict(sealed))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_data_never_overrides_a_sealed_claim(core: CoreFacade) -> None:
    _register(core, "acme.prefs", {"nickname": "ann"})
    sealed = await core.collect_claims(uuid4())
    assert sealed["nickname"] == "ann"
    with use_active_core(core), pytest.raises(UnsealedClaimsError, match="nickname"):
        tokens.create_access_token({**_envelope(), "nickname": "eve"}, claims=sealed)


def test_an_unprotected_key_in_data_is_minted(core: CoreFacade) -> None:
    with use_active_core(core):
        token = tokens.create_access_token({**_envelope(), "locale": "en"})
        payload = tokens.decode_token(token)
    assert payload is not None and payload["locale"] == "en"


def test_r017_is_in_the_catalogue() -> None:
    entry = error_codes.get("SANDLOOM-R017")
    assert entry.slug == "unsealed-claims"
    assert entry.kind == "runtime"


# --------------------------------------------------------------------------- #
# collect_claims seals its inputs: the issuer and precomputed (C-a2)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_issuer_is_never_a_string(core: CoreFacade) -> None:
    with pytest.raises(UnsealedClaimsError, match="not a name"):
        await core.collect_claims(uuid4(), object(), issuer="idp")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_issuer_is_a_registered_contributor(core: CoreFacade) -> None:
    async def stray(_user_id: Any) -> dict[str, Any]:
        return {}

    with pytest.raises(UnsealedClaimsError, match="not a claims contributor"):
        await core.collect_claims(uuid4(), object(), issuer=stray)


@pytest.mark.asyncio
async def test_the_issuer_is_resolved_from_the_registering_module() -> None:
    """The issuer's fields pass the check only for the module that
    registered the contributor it names: ``acme.rogue``'s own contributor
    cannot mint the issuer's ``email``."""
    core = _core()
    idp = _register(core, IDP, {"roles": []}, critical=True)
    rogue = _register(core, ROGUE, {})
    fields = {"email": "u@example.com", "email_verified": True}
    sealed = await core.collect_claims(uuid4(), issuer=idp, issuer_claims=fields)
    assert sealed["email"] == "u@example.com"
    assert "idp" in sealed["claims_sources"]
    with pytest.raises(ClaimsUnavailable) as refused:
        await core.collect_claims(uuid4(), issuer=rogue, issuer_claims=fields)
    assert refused.value.owner == "rogue"


@pytest.mark.asyncio
async def test_precomputed_needs_the_issuer_and_its_session(core: CoreFacade) -> None:
    idp = core._contributors_of("idp")[0]
    with pytest.raises(UnsealedClaimsError, match="only with issuer"):
        await core.collect_claims(uuid4(), precomputed={idp: {"roles": ["x"]}})
    with pytest.raises(UnsealedClaimsError, match="only with issuer"):
        await core.collect_claims(
            uuid4(), issuer=idp, precomputed={idp: {"roles": ["x"]}}
        )


@pytest.mark.asyncio
async def test_precomputed_is_the_issuers_own_contributor(core: CoreFacade) -> None:
    idp = core._contributors_of("idp")[0]
    grants = core._contributors_of("grants")[0]
    with pytest.raises(UnsealedClaimsError, match="not a claims contributor of"):
        await core.collect_claims(
            uuid4(), object(), issuer=idp, precomputed={grants: {"tenant_ids": []}}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "contributed",
    [{"tenant_ids": [TENANT]}, {"nickname": "eve"}, {"claims_sources": ["idp"]}],
)
async def test_precomputed_carries_only_the_issuers_assigned_keys(
    core: CoreFacade, contributed: dict[str, Any]
) -> None:
    idp = core._contributors_of("idp")[0]
    with pytest.raises(UnsealedClaimsError, match="does not assign"):
        await core.collect_claims(
            uuid4(), object(), issuer=idp, precomputed={idp: contributed}
        )


@pytest.mark.asyncio
async def test_precomputed_replaces_the_issuers_call(core: CoreFacade) -> None:
    idp = core._contributors_of("idp")[0]
    sealed = await core.collect_claims(
        uuid4(), object(), issuer=idp, precomputed={idp: {"roles": ["cas"]}}
    )
    assert sealed["roles"] == ["cas"]
    assert idp.calls == 0
    assert "idp" in sealed["claims_sources"]


# --------------------------------------------------------------------------- #
# T-11: the service token grants no user rights
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", SEALED_ONLY)
def test_a_service_token_refuses_every_sealed_key(core: CoreFacade, key: str) -> None:
    with use_active_core(core), pytest.raises(UnsealedClaimsError, match=key):
        tokens.create_service_token({**_envelope(), key: ["admin"]})


@pytest.mark.asyncio
@pytest.mark.usefixtures("mem_cache")
async def test_a_service_token_is_denied_by_every_gate(core: CoreFacade) -> None:
    with use_active_core(core):
        token = tokens.create_service_token({**_envelope(), "scope": "billing"})
        payload = tokens.decode_token(token)
    assert payload is not None
    # The kernel's own provenance and the current cpv: absent protected
    # claims are a definite deny, never "refresh" (token_policy_outdated).
    assert payload["claims_sources"] == []
    assert payload["claims_sources_failed"] == []
    assert payload["cpv"] == {
        "v": CPV_VERSION,
        "k": dict(core.claim_authorities.digests),
    }
    assert view.UNTRUSTED_KEY not in payload
    assert payload["scope"] == "billing"
    got = await statuses(core, token)
    assert {k: v for k, v in got.items() if k != "/tenant"} == {
        k: v for k, v in ALL_403.items() if k != "/tenant"
    }
    assert got["/tenant"] in (400, 403)


# --------------------------------------------------------------------------- #
# T-12: the kernel test authority
# --------------------------------------------------------------------------- #


def _admin_claims() -> dict[str, Any]:
    return {
        **_envelope(),
        "roles": ["admin"],
        "permissions": ["x.read"],
        "tenant_id": TENANT,
        "tenant_ids": [TENANT],
    }


@pytest.mark.asyncio
@pytest.mark.usefixtures("mem_cache")
async def test_a_test_token_passes_every_gate_in_a_test_app(core: CoreFacade) -> None:
    assert view.test_authority_enabled()
    assert await statuses(core, mint_test_token(_admin_claims())) == ALL_200
    _, headers = make_user_headers(roles=["admin"], permissions=["x.read"])
    bearer = headers["Authorization"].removeprefix("Bearer ")
    assert (await statuses(core, bearer))["/role"] == 200


@pytest.mark.asyncio
@pytest.mark.usefixtures("mem_cache")
async def test_a_test_stating_claims_sources_gets_that_provenance_checked(
    core: CoreFacade,
) -> None:
    """``roles`` stated from ``rogue``: the map's authority is ``idp``, so
    the roles are untrusted (403); stated from ``idp`` they are trusted."""
    claims = {**_admin_claims(), "claims_sources_failed": []}
    rogue = mint_test_token({**claims, "claims_sources": ["rogue", "grants"]})
    got = await statuses(core, rogue)
    assert got["/role"] == got["/admin"] == got["/perm"] == 403
    assert got["/tenant"] == 200
    idp = mint_test_token({**claims, "claims_sources": ["idp", "grants"]})
    assert await statuses(core, idp) == ALL_200
    failed = mint_test_token(
        {
            **claims,
            "claims_sources": ["idp", "grants"],
            "claims_sources_failed": ["idp"],
        }
    )
    assert (await statuses(core, failed))["/role"] == 403


def test_the_test_authority_is_refused_outside_a_test_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(view, "_test_composition", [False])
    for mint in (
        lambda: sealed_claims({"roles": ["admin"]}),
        lambda: mint_test_token(_admin_claims()),
        lambda: make_user_headers(roles=["admin"]),
    ):
        with pytest.raises(UnsealedClaimsError, match="test composition"):
            mint()


@pytest.mark.asyncio
@pytest.mark.usefixtures("mem_cache")
async def test_a_test_token_grants_nothing_outside_a_test_composition(
    core: CoreFacade, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = mint_test_token(_admin_claims())
    monkeypatch.setattr(view, "_test_composition", [False])
    got = await statuses(core, token)
    assert got["/role"] != 200 and got["/admin"] != 200 and got["/tenant"] != 200


def test_sealed_claims_states_a_cpv_or_none() -> None:
    assert sealed_claims({"roles": []})["cpv"] == view.TEST_CPV
    assert sealed_claims({"roles": []})["claims_sources"] == [view.TEST_AUTHORITY]
    assert "cpv" not in sealed_claims({"roles": [], "cpv": None})
    stated = sealed_claims({"roles": [], "claims_sources": ["idp"]})
    assert stated["claims_sources"] == ["idp"]
