"""The sealed mint, service tokens and test tokens (ADR-0014 decision 5,
owner answer #13, adversarial C-a2; SANDLOOM-R017).

* T-11: code in the issuer process that hands ``create_access_token``
  protected claims, provenance or ``cpv`` outside the sealed
  ``collect_claims`` result is refused; a service token
  (``create_service_token``) authenticates and every gate answers 403.
* ``collect_claims`` seals its inputs: the issuer is resolved from the
  module that registered the contributor it is identified by (never a
  string), its session is the map's token issuer's only, and no claim
  value is taken from the caller (SEC-2: every claim is a contributor's).
* T-12: ``as_user`` / ``mint_test_token`` grant rights through the kernel
  test authority in a test composition and are refused outside one; a test
  that states ``claims_sources`` gets that provenance checked.

Kernel-only: fake module paths on a facade with fake databases (no I/O
beyond the in-memory revocation cache).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from types import SimpleNamespace
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
from sandloom.core.facade import ClaimsUnavailable, CoreFacade, CoreRuntime
from sandloom.core.runtime import use_active_core
from sandloom.core.testing.auth import make_user_headers, mint_test_token, sealed_claims
from sandloom.core.tests import test_auth_rotation_revocation as _revocation
from sandloom.core.tests.test_claims_split_matrix import GATES, statuses

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

#: Keys modules of this composition declare without the map naming them
#: (a contributor's fail-safe declaration or an imported claim atom's key;
#: the kernel names none since B-N12): protected all the same.
DECLARED = frozenset({"subscription_status", "plan_code", "features", "limits"})

#: Every key a mint takes only from a sealed result: the protected claims
#: (base, an application key the map assigns, a declared key), the tenant
#: selectors, provenance, ``cpv``, the subject structure and the authority
#: binding.
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


class _FakeUoW:
    def __init__(self) -> None:
        """A unit whose session has an empty ``info`` mapping."""
        self.session = SimpleNamespace(info={})

    async def __aenter__(self) -> _FakeUoW:
        """Enter the unit; nothing to open."""
        return self

    async def __aexit__(self, *exc: Any) -> None:
        """Leave the unit; nothing to commit."""
        return None


class _FakeDatabase:
    def uow_factory(self) -> Any:
        """The fake unit-of-work class."""
        return _FakeUoW

    def outbox(self) -> None:
        """No outbox."""
        return None

    def audit(self) -> None:
        """No audit sink."""
        return None


class _Counted:
    """A contributor answering ``result`` and counting its calls."""

    def __init__(self, result: dict[str, Any]) -> None:
        """Remember the answer."""
        self.result = result
        self.calls = 0

    async def __call__(self, user_id: Any) -> dict[str, Any]:
        """Answer and count."""
        self.calls += 1
        return dict(self.result)


def _register(
    core: CoreFacade, path: str, result: dict[str, Any], *, critical: bool = False
) -> _Counted:
    fn = _Counted(result)
    with core.module_registration(path) as api:
        api.add_claims_contributor(fn, critical=critical)
    return fn


def _core() -> CoreFacade:
    core = CoreFacade(app=FastAPI())
    core.database = lambda name="default": _FakeDatabase()  # type: ignore[method-assign]
    policy = dataclasses.replace(build_claim_authorities(MAP), declared=DECLARED)
    core._assign_claim_authorities(policy)
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
# collect_claims seals its inputs: the issuer and its session (C-a2)
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
@pytest.mark.parametrize("given", ["precomputed", "issuer_claims"])
async def test_collect_claims_takes_no_claim_value(
    core: CoreFacade, given: str
) -> None:
    """SEC-2: no entry hands the mint a claim value. ``precomputed`` and
    ``issuer_claims`` are gone (no alias), on the facade and on the
    ``CoreRuntime`` view module code holds; nothing runs."""
    idp = core._contributors_of("idp")[0]
    value = {idp: {"roles": ["owner"]}} if given == "precomputed" else {"email": "e"}
    with pytest.raises(TypeError, match=given):
        await core.collect_claims(uuid4(), object(), issuer=idp, **{given: value})
    with pytest.raises(TypeError, match=given):
        await CoreRuntime(core).collect_claims(
            uuid4(), object(), issuer=idp, **{given: value}
        )
    assert idp.calls == 0


@pytest.mark.asyncio
async def test_the_issuers_fields_come_from_its_own_contributor() -> None:
    """``email`` / ``email_verified`` are what the issuer's contributor
    returned in this pass, run once in the issuer's session."""
    core = _core()
    fields = {"roles": [], "email": "u@example.com", "email_verified": True}
    idp = _register(core, IDP, fields, critical=True)
    sealed = await core.collect_claims(uuid4(), object(), issuer=idp)
    assert sealed["email"] == "u@example.com" and sealed["email_verified"] is True
    assert "idp" in sealed["claims_sources"]
    assert idp.calls == 1


@pytest.mark.asyncio
async def test_another_module_writing_the_issuers_fields_is_a_violator() -> None:
    """``acme.rogue`` writes ``email``: optional, it loses its whole result;
    critical, it refuses the mint. The issuer's value is never replaced."""
    core = _core()
    idp = _register(
        core, IDP, {"email": "u@example.com", "email_verified": False}, critical=True
    )
    _register(core, ROGUE, {"email": "eve@example.com", "email_verified": True})
    sealed = await core.collect_claims(uuid4(), object(), issuer=idp)
    assert sealed["email"] == "u@example.com" and sealed["email_verified"] is False
    assert sealed["claims_sources_failed"] == ["rogue"]

    critical = _core()
    idp = _register(critical, IDP, {"email": "u@example.com"}, critical=True)
    _register(critical, ROGUE, {"email_verified": True}, critical=True)
    with pytest.raises(ClaimsUnavailable) as refused:
        await critical.collect_claims(uuid4(), object(), issuer=idp)
    assert refused.value.owner == "rogue"


# --------------------------------------------------------------------------- #
# the mint issuer binding (SEC-2): the issuer session is the map's issuer's
# only, identified by its short name AND import path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_authority_that_is_not_the_issuer_takes_no_issuer_session(
    core: CoreFacade,
) -> None:
    """``acme.grants`` is the authority of ``tenant_ids`` but not the
    issuer: naming its own contributor as ``issuer`` with a stand-in session
    is refused before anything runs."""
    grants = core._contributors_of("grants")[0]
    with pytest.raises(UnsealedClaimsError, match="not the claim_authorities issuer"):
        await core.collect_claims(uuid4(), object(), issuer=grants)
    assert grants.calls == 0


@pytest.mark.asyncio
async def test_a_module_named_like_the_issuer_from_another_path_is_not_it() -> None:
    """``evil.idp`` registers under the issuer's short name ``idp``: the map
    names ``acme.idp``, so its session is refused before anything runs."""
    core = _core()
    impostor = _register(core, "evil.idp", {"roles": []}, critical=True)
    with pytest.raises(UnsealedClaimsError, match="not the claim_authorities issuer"):
        await core.collect_claims(uuid4(), object(), issuer=impostor)
    assert impostor.calls == 0


@pytest.mark.asyncio
async def test_a_process_whose_map_names_no_issuer_takes_no_issuer_session() -> None:
    """A process that does not compose the map's issuer (the filtered map
    names none) runs no contributor in a caller's session."""
    core = CoreFacade(app=FastAPI())
    core.database = lambda name="default": _FakeDatabase()  # type: ignore[method-assign]
    core._assign_claim_authorities(build_claim_authorities({"roles": IDP}))
    idp = _register(core, IDP, {"roles": ["admin"]}, critical=True)
    with pytest.raises(UnsealedClaimsError, match="names no issuer"):
        await core.collect_claims(uuid4(), object(), issuer=idp)
    assert idp.calls == 0
    # Without a session the contributor runs in its own unit, as any other.
    sealed = await core.collect_claims(uuid4(), issuer=idp)
    assert sealed["roles"] == ["admin"]


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
