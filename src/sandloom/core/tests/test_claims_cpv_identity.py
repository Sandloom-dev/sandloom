"""The ``cpv`` identity of an authority (ADR-0014 decision 4, owner decision
2026-11-09).

A key's ``cpv`` digest is computed from the key and the canonical identity of
the authority ``claim_authorities`` assigns: its full import path, the same
whether the module is loaded here or reached over RPC. For a remote authority
the pinned ``service_id`` (the pair MAC) and the authority binding (the
answer names the module serving it) are checked on their own and never
replace the path in ``cpv``.

1. mono and split stamp and accept the same digest for one assignment;
2. a service other than the pinned one is refused, even running the
   assigned module;
3. another module behind the assigned short name, on the right service, is
   refused by the authority binding;
4. a new implementation behind the same import path keeps the digest (the
   documented rule; its invalidation path is a stated gap);
5. a ``previous=`` rollout keeps its semantics in mixed local/remote
   placements.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from sandloom.core import rpc
from sandloom.core.app_factory.claim_authorities import (
    build_claim_authorities,
    filter_to_composition,
)
from sandloom.core.auth import claims_view as view
from sandloom.core.claims import (
    AUTHORITY_CLAIM,
    Authority,
    ClaimAuthorities,
    cpv_digest,
)
from sandloom.core.config.settings import settings
from sandloom.core.facade import ClaimsUnavailable, CoreFacade
from sandloom.core.rpc import RemoteServiceProxy
from sandloom.core.tests import test_rpc_keyed_binding as _keyed

# The keyed RPC harness: the process settings are the caller (``billing``);
# ``tenancy`` and ``rogue`` are providers with their own X25519 keys.
caller = _keyed.caller

IDP = "acme.idp"
GRANTS = "acme.grants"
EVIL = "evil.grants"  # another module under the short name ``grants``
CONTRACT = "grants.claims.v1"
MAP: dict[str, Any] = {"issuer": IDP, "roles": IDP, "tenant_ids": GRANTS}
NOW = 2_000_000_000.0


def _issuer(bindings: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> CoreFacade:
    """The issuing process: ``acme.idp`` loaded, ``grants`` bound remote."""
    core = CoreFacade(app=FastAPI())
    core.database = lambda name="default": _keyed._Database()  # type: ignore[method-assign]
    loaded = {"idp": IDP}
    core._assign_claim_authorities(
        filter_to_composition(
            build_claim_authorities(MAP), loaded=loaded, bindings=bindings
        )
    )

    async def roles(_user_id: Any) -> dict[str, Any]:
        return {"roles": ["admin"]}

    with core.module_registration(IDP) as api:
        api.add_claims_contributor(roles, critical=True)
    monkeypatch.setattr(core.settings, "service_bindings", bindings)
    return core


def _provider(service: str, module: str = GRANTS) -> FastAPI:
    """A provider service ``service`` running ``module`` (short ``grants``)."""
    cfg = settings.model_copy(
        update={
            "rpc_service_id": service,
            "rpc_private_key": None,
            "rpc_key_id": None,
            "rpc_dh_private_key": _keyed.DH[service][0],
            "rpc_dh_key_id": {"tenancy": "t1", "rogue": "r1"}[service],
            "rpc_allowed_calls": {"billing": [CONTRACT]},
        }
    )
    app = FastAPI()
    core = CoreFacade(app=app, settings_obj=cfg)
    app.state.core = core
    core.database = lambda name="default": _keyed._Database()  # type: ignore[method-assign]

    async def grants(_user_id: Any) -> dict[str, Any]:
        return {"tenant_ids": ["t"]}

    with core.module_registration(module) as api:
        api.add_claims_contributor(grants, critical=True)
    rpc.install_service_rpc_routes(app, core)
    return app


def _resolver(provider: FastAPI) -> Callable[..., RemoteServiceProxy]:
    def resolve(name: str, binding: dict[str, Any], **_: Any) -> RemoteServiceProxy:
        return RemoteServiceProxy(
            service_name=name,
            binding=binding,
            default_timeout_seconds=5.0,
            client=AsyncClient(
                transport=ASGITransport(app=provider), base_url="http://grants"
            ),
        )

    return resolve


def _binding(service_id: str) -> dict[str, Any]:
    return {CONTRACT: {"base_url": "http://grants", "service_id": service_id}}


@pytest.fixture(autouse=True)
async def _close_shared_client() -> AsyncIterator[None]:
    yield
    await rpc.close_rpc_client()


def _mono_issuer() -> CoreFacade:
    """The monolith: both authorities loaded in the issuing process."""
    core = CoreFacade(app=FastAPI())
    core.database = lambda name="default": _keyed._Database()  # type: ignore[method-assign]
    core._assign_claim_authorities(
        filter_to_composition(
            build_claim_authorities(MAP),
            loaded={"idp": IDP, "grants": GRANTS},
            bindings={},
        )
    )

    async def roles(_user_id: Any) -> dict[str, Any]:
        return {"roles": ["admin"]}

    async def grants(_user_id: Any) -> dict[str, Any]:
        return {"tenant_ids": ["t"]}

    with core.module_registration(IDP) as api:
        api.add_claims_contributor(roles, critical=True)
    with core.module_registration(GRANTS) as api:
        api.add_claims_contributor(grants, critical=True)
    return core


# --- 1. mono and split: one digest -----------------------------------------
@pytest.mark.asyncio
async def test_mono_and_split_stamp_and_accept_the_same_cpv(
    caller: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    mono = await _mono_issuer().collect_claims(uuid4())
    split_core = _issuer(_binding("tenancy"), monkeypatch)
    split_core.set_service_resolver(_resolver(_provider("tenancy")))
    split = await split_core.collect_claims(uuid4())

    expected = {
        "roles": cpv_digest("roles", IDP),
        "tenant_ids": cpv_digest("tenant_ids", GRANTS),
    }
    for minted in (mono, split):
        assert minted["cpv"]["k"]["roles"] == expected["roles"]
        assert minted["cpv"]["k"]["tenant_ids"] == expected["tenant_ids"]
        assert minted["tenant_ids"] == ["t"]
        assert AUTHORITY_CLAIM not in minted
    assert mono["cpv"] == split["cpv"]
    # Every verifier placement accepts the same digest: the issuer, the
    # grants service alone, a service running neither.
    whole = build_claim_authorities(MAP)
    for loaded, bindings in (
        ({"idp": IDP, "grants": GRANTS}, {}),
        ({"idp": IDP}, _binding("tenancy")),
        ({"grants": GRANTS}, {}),
        ({}, {}),
    ):
        policy = filter_to_composition(whole, loaded=loaded, bindings=bindings)
        assert dict(policy.accepted)["tenant_ids"] == (
            ("grants", expected["tenant_ids"]),
        )
    # The service_id is not part of the digest: the same assignment pinned
    # to another service stamps the same cpv.
    moved = filter_to_composition(
        whole, loaded={"idp": IDP}, bindings=_binding("rogue")
    )
    assert dict(moved.digests) == dict(split_core._claim_authorities.digests)


# --- 2. the pinned service_id --------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pinned", "answering"),
    [("rogue", "tenancy"), ("tenancy", "rogue")],
    ids=["pinned-to-another-service", "answered-by-another-service"],
)
async def test_a_service_other_than_the_pinned_one_is_refused(
    caller: Any, monkeypatch: pytest.MonkeyPatch, pinned: str, answering: str
) -> None:
    """The answering service runs the assigned module (``acme.grants``, the
    map's path): the pair MAC still refuses it, so the critical authority
    refuses the mint (503)."""
    core = _issuer(_binding(pinned), monkeypatch)
    core.set_service_resolver(_resolver(_provider(answering)))
    with pytest.raises(ClaimsUnavailable) as refused:
        await core.collect_claims(uuid4())
    assert refused.value.status_code == 503
    assert refused.value.owner == "grants"


# --- 3. the authority binding ---------------------------------------------
@pytest.mark.asyncio
async def test_another_module_behind_the_short_name_is_refused(
    caller: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The right service (pinned, MAC-verified) runs ``evil.grants`` under
    the short name ``grants``: the service_id proves the service, not the
    module; the answer's authority binding refuses it."""
    core = _issuer(_binding("tenancy"), monkeypatch)
    core.set_service_resolver(_resolver(_provider("tenancy", module=EVIL)))
    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(ClaimsUnavailable) as refused,
    ):
        await core.collect_claims(uuid4())
    assert refused.value.owner == "grants"
    assert "answered over RPC as 'evil.grants'" in caplog.text
    assert "'acme.grants'" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [{"tenant_ids": ["t"]}, {"tenant_ids": ["t"], AUTHORITY_CLAIM: EVIL}],
    ids=["names-no-module", "names-another-module"],
)
async def test_an_answer_not_naming_the_assigned_module_fails_the_step(
    monkeypatch: pytest.MonkeyPatch, answer: dict[str, Any]
) -> None:
    """A provider older than the binding names no module: refused too
    (upgrade every ``<m>.claims.v1`` provider before the issuer)."""

    class _Answer:
        async def contribute(self, _user_id: Any) -> dict[str, Any]:
            return dict(answer)

    core = _issuer({CONTRACT: {"base_url": "http://grants"}}, monkeypatch)
    core.set_service_resolver(lambda name, binding, **_: _Answer())
    with pytest.raises(ClaimsUnavailable):
        await core.collect_claims(uuid4())


@pytest.mark.asyncio
async def test_an_owner_assigned_nothing_is_not_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote owner of unprotected keys only is not an authority: it is
    not bound (the mint check keeps it off every protected key)."""

    class _Answer:
        async def contribute(self, _user_id: Any) -> dict[str, Any]:
            return {"loyalty_tier": "gold"}

    bindings = {
        CONTRACT: {"base_url": "http://grants"},
        "loyalty.claims.v1": {"base_url": "http://loyalty", "critical": False},
    }
    answers = {
        CONTRACT: _Grants(),
        "loyalty.claims.v1": _Answer(),
    }
    core = _issuer(bindings, monkeypatch)
    core.set_service_resolver(lambda name, binding, **_: answers[name])
    claims = await core.collect_claims(uuid4())
    assert claims["loyalty_tier"] == "gold"
    assert claims["claims_sources_failed"] == []


class _Grants:
    async def contribute(self, _user_id: Any) -> dict[str, Any]:
        return {"tenant_ids": ["t"], AUTHORITY_CLAIM: GRANTS}


# --- 4. a new implementation behind the same import path --------------------
def test_the_digest_is_the_assignment_not_the_implementation() -> None:
    """The documented rule (ADR-0014 decision 4): ``cpv`` fingerprints the
    assignment ``(key, import path)``. A new implementation shipped behind
    the same path, or the same authority moved to another service, keeps
    every digest, so tokens minted before stay trusted. A real change of the
    security authority behind one path is therefore not invalidated by
    ``cpv``: the invalidation path today is to assign it under a new import
    path (a key change with ``previous=``, one refresh per client) or to end
    every session (JWT key rotation without the old key). A revision of the
    assignment that changes the digest without a new path is a recorded gap
    (ADR-0014 decision 4, "authority revision")."""
    before = build_claim_authorities(MAP)
    after = build_claim_authorities(dict(MAP))
    assert dict(before.digests) == dict(after.digests)
    renamed = build_claim_authorities({**MAP, "tenant_ids": "acme.grants_v2"})
    assert renamed.digests["tenant_ids"] != before.digests["tenant_ids"]
    assert renamed.digests["roles"] == before.digests["roles"]


# --- 5. previous= in mixed local/remote placement ----------------------------
ID = "sandloom_identity"
ACME = "acme.modules.acme_identity"
OLD_MAP: dict[str, Any] = {"roles": ID}
NEW_MAP: dict[str, Any] = {
    "roles": Authority(ACME, previous=Authority("identity", module=ID))
}
LIFETIME, LEEWAY = 900.0, 10.0


def _token(owner: str, path: str) -> dict[str, Any]:
    return {
        "sub": "u",
        "token_type": "access",
        "roles": ["admin"],
        "iat": NOW - 60,
        "exp": NOW - 60 + LIFETIME,
        "claims_sources": [owner],
        "claims_sources_failed": [],
        "cpv": {"v": 1, "k": {"roles": cpv_digest("roles", path)}},
    }


def _verifier(
    mapping: dict[str, Any], loaded: dict[str, str], bindings: dict[str, Any]
) -> ClaimAuthorities:
    policy = filter_to_composition(
        build_claim_authorities(mapping), loaded=loaded, bindings=bindings
    )
    return dataclasses.replace(policy, effective_at=NOW - 30)


def _roles_reason(payload: dict[str, Any], policy: ClaimAuthorities) -> str | None:
    view.apply_view(
        payload, policy, now=NOW, lifetime_seconds=LIFETIME, leeway_seconds=LEEWAY
    )
    return view.untrusted_reasons(payload).get("roles")


#: Where the new authority runs relative to a verifier: loaded with it,
#: reached over RPC, or neither (a service that only reads tokens).
PLACEMENTS = {
    "acme-local": ({"acme_identity": ACME}, {}),
    "acme-remote": ({}, {"acme_identity.claims.v1": {"service_id": "acme"}}),
    "identity-local": ({"identity": ID}, {}),
    "neither": ({}, {}),
}


@pytest.mark.parametrize("placement", sorted(PLACEMENTS))
def test_a_previous_rollout_reads_alike_in_every_placement(placement: str) -> None:
    loaded, bindings = PLACEMENTS[placement]
    old = _token("identity", ID)
    new = _token("acme_identity", ACME)
    # Step 1-2: the new map with previous= accepts both tokens.
    during = _verifier(NEW_MAP, loaded, bindings)
    assert _roles_reason(dict(old), during) is None
    assert _roles_reason(dict(new), during) is None
    # A forged mix stays out: the old digest under the new source.
    mixed = _token("acme_identity", ID)
    assert _roles_reason(mixed, during) == view.SOURCE_ABSENT
    # Step 3: previous= removed; the old token needs one refresh (401).
    after = _verifier({"roles": ACME}, loaded, bindings)
    assert _roles_reason(dict(old), after) == view.OUTDATED
    assert _roles_reason(dict(new), after) is None
    # A verifier still on the old map, wherever it runs: the new token was
    # minted before it took effect here, so a refresh is asked, never a loop.
    before = _verifier(OLD_MAP, loaded, bindings)
    assert _roles_reason(dict(new), before) == view.OUTDATED
    assert _roles_reason(dict(old), before) is None
