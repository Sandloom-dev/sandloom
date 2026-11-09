"""A token claim has one writer: the violator rule (ADR-0014 decision 2).

``require_platform_admin`` and the platform console's ``platform_operator``
trust ``roles`` when ``"identity"`` is in ``claims_sources``. That provenance
names the owners that RAN, not the writer of each key: before PR #70 the merge
was a plain ``dict.update`` in plan order, so any composed module returning
``{"roles": ["admin"]}`` replaced identity's roles in a token still stamped
``claims_sources: ["identity", ...]``: platform administrator. The same held
for the token envelope a contributor could pre-fill (``iat_us`` past every
revocation mark, ``auth_time`` past step-up, ``jti`` / ``iss`` / ``aud``,
which ``create_access_token`` only ``setdefault``\\ s).

PR #70 refused every such mint (503), so one buggy optional module denied
every login (problem #14). Now the composition assigns each protected claim to
its authority (the standard map here: identity for ``roles`` /
``permissions``, tenancy for ``tenant_ids``), and a contributor writing a
protected key it is not assigned, or a reserved key, is a violator: an
optional one loses its whole result (``claims_sources_failed``, ERROR, a
counter) and the token carries the authorities' values; a critical one
refuses the mint naming itself. Identity's tokens are unchanged.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException

from sandloom.core.app_factory.claim_authorities import build_claim_authorities
from sandloom.core.facade import ClaimsUnavailable, CoreFacade
from sandloom.core.facade._claims import CLAIMS_VIOLATIONS_METRIC, RESERVED_CLAIM_KEYS
from sandloom.shared.guards.platform import require_platform_admin
from sandloom.shared.subject import UserContext


class _FakeSession:
    def __init__(self) -> None:
        """Session stand-in with an empty ``info`` mapping."""
        self.info: dict[str, Any] = {}


class _FakeUoW:
    def __init__(self) -> None:
        """Unit-of-work stand-in holding a fake session."""
        self.session = _FakeSession()

    async def __aenter__(self) -> _FakeUoW:
        """Enter the unit; nothing to open."""
        return self

    async def __aexit__(self, *exc: Any) -> None:
        """Leave the unit; nothing to commit or roll back."""
        return None


class _FakeDatabase:
    def uow_factory(self) -> Any:
        """The fake unit-of-work class."""
        return _FakeUoW

    def outbox(self) -> None:
        """No outbox in these tests."""
        return None

    def audit(self) -> None:
        """No audit sink in these tests."""
        return None


#: The standard batteries' authorities (``sandloom_standard.main``'s map).
STANDARD = build_claim_authorities(
    {
        "issuer": "sandloom_identity",
        "roles": "sandloom_identity",
        "permissions": "sandloom_identity",
        "tenant_ids": "sandloom_tenancy",
    }
)


def _core() -> CoreFacade:
    """A kernel facade whose databases are fakes (claims need no I/O here)."""
    core = CoreFacade(app=FastAPI())
    core.database = lambda name="default": _FakeDatabase()  # type: ignore[method-assign]
    core._assign_claim_authorities(STANDARD)
    return core


class _Proxy:
    """A remote ``<owner>.claims.v1`` client (what RPC would return)."""

    def __init__(self, result: dict[str, Any]) -> None:
        """Remember the claims the remote owner answers with."""
        self._result = result

    async def contribute(self, user_id: Any) -> dict[str, Any]:
        """Answer like ``<owner>.claims.v1.contribute`` over RPC."""
        return self._result


async def _identity(user_id: Any) -> dict[str, Any]:
    """Identity's claims: it always writes ``roles`` and ``permissions``."""
    return {"roles": ["member"], "permissions": []}


def _with_identity(core: CoreFacade) -> None:
    """Register identity's critical claims contributor on ``core``."""
    with core.module_registration("sandloom_identity") as api:
        api.add_claims_contributor(_identity, critical=True)


async def _is_platform_admin(claims: dict[str, Any]) -> bool:
    """Whether ``require_platform_admin`` lets a subject with these claims through."""
    subject = UserContext.model_validate({**claims, "sub": str(uuid4())})
    try:
        await require_platform_admin(subject=subject)
    except HTTPException as exc:
        assert exc.status_code == 403
        return False
    return True


@pytest.mark.asyncio
async def test_identity_alone_keeps_its_roles_and_gate() -> None:
    """Identity's own claims are unchanged, and the gate still reads them."""
    core = _core()

    async def admin(user_id: Any) -> dict[str, Any]:
        """Identity answering with an admin role."""
        return {"roles": ["admin"], "permissions": ["platform.read"]}

    with core.module_registration("sandloom_identity") as api:
        api.add_claims_contributor(admin, critical=True)
    claims = await core.collect_claims(uuid4(), object(), issuer="identity")
    assert claims["roles"] == ["admin"]
    assert claims["claims_sources"] == ["identity"]
    assert await _is_platform_admin(claims)


@pytest.mark.asyncio
async def test_one_owners_contributors_still_merge() -> None:
    """Two contributors of the SAME owner may write the same key (last wins)."""
    core = _core()

    async def first(user_id: Any) -> dict[str, Any]:
        """First contributor of the owner."""
        return {"roles": ["member"]}

    async def second(user_id: Any) -> dict[str, Any]:
        """Second contributor of the same owner."""
        return {"roles": ["member", "editor"], "permissions": []}

    with core.module_registration("sandloom_identity") as api:
        api.add_claims_contributor(first, critical=True)
        api.add_claims_contributor(second, critical=True)
    claims = await core.collect_claims(uuid4(), object(), issuer="identity")
    assert claims["roles"] == ["member", "editor"]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["roles", "permissions"])
async def test_monolith_module_cannot_write_identitys_claims(key: str) -> None:
    """Monolith: a module loaded after identity returns ``roles: [admin]``.

    Before PR #70 this token carried ``roles == ["admin"]`` with
    ``claims_sources == ["identity", "rogue"]`` and passed the platform gate;
    under PR #70 every login was refused. Now the login gets identity's values.
    """
    core = _core()
    _with_identity(core)

    async def rogue(user_id: Any) -> dict[str, Any]:
        """A non-identity module writing a key identity is the authority of."""
        return {key: ["admin"], "plan": "pro"}

    with core.module_registration("acme.rogue") as api:
        api.add_claims_contributor(rogue)  # not critical
    claims = await core.collect_claims(uuid4(), object(), issuer="identity")
    assert claims[key] == ([] if key == "permissions" else ["member"])
    assert "plan" not in claims  # its whole result is dropped
    assert claims["claims_sources"] == ["identity"]
    assert claims["claims_sources_failed"] == ["rogue"]
    assert core.get_metric(CLAIMS_VIOLATIONS_METRIC) == 1
    assert not await _is_platform_admin(claims)


@pytest.mark.asyncio
async def test_monolith_order_does_not_matter() -> None:
    """A module registered BEFORE identity is the violator too, never identity."""
    core = _core()

    async def rogue(user_id: Any) -> dict[str, Any]:
        """A non-identity module writing a key identity is the authority of."""
        return {"roles": ["admin"]}

    with core.module_registration("acme.rogue") as api:
        api.add_claims_contributor(rogue)
    _with_identity(core)
    claims = await core.collect_claims(uuid4(), object(), issuer="identity")
    assert claims["roles"] == ["member"]
    assert claims["claims_sources_failed"] == ["rogue"]


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [True, False], ids=["rogue-first", "rogue-last"])
async def test_a_critical_violator_refuses_the_mint_naming_itself(first: bool) -> None:
    """A critical violator: 503 naming itself, in either registration order."""
    core = _core()

    async def rogue(user_id: Any) -> dict[str, Any]:
        """A critical non-identity module writing ``roles``."""
        return {"roles": ["admin"]}

    if not first:
        _with_identity(core)
    with core.module_registration("acme.rogue") as api:
        api.add_claims_contributor(rogue, critical=True)
    if first:
        _with_identity(core)
    with pytest.raises(ClaimsUnavailable) as exc_info:
        await core.collect_claims(uuid4(), object(), issuer="identity")
    assert exc_info.value.status_code == 503
    assert exc_info.value.owner == "rogue"


@pytest.mark.asyncio
async def test_split_remote_owner_cannot_write_identitys_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Split deploy: identity mints alone; a ``rogue.claims.v1`` binding answers
    ``roles: [admin]`` over RPC.

    Remote owners run after every local one, so before PR #70 the remote
    answer always won, and the identity-signed token was accepted by every
    service's platform gate. Now its whole answer is dropped.
    """
    core = _core()
    _with_identity(core)
    monkeypatch.setattr(
        core.settings,
        "service_bindings",
        {"rogue.claims.v1": {"base_url": "http://rogue", "critical": False}},
    )
    proxy = _Proxy({"roles": ["admin"], "plan": "pro"})
    core.set_service_resolver(lambda name, binding, **_: proxy)
    claims = await core.collect_claims(uuid4(), object(), issuer="identity")
    assert claims["roles"] == ["member"]
    assert "plan" not in claims
    assert claims["claims_sources_failed"] == ["rogue"]
    assert not await _is_platform_admin(claims)


@pytest.mark.asyncio
async def test_split_remote_owner_with_its_own_keys_still_contributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote authority writing only its own keys (tenancy's ``tenant_ids``) still contributes and grants nothing."""
    core = _core()
    _with_identity(core)
    monkeypatch.setattr(
        core.settings,
        "service_bindings",
        {"tenancy.claims.v1": {"base_url": "http://tenancy"}},
    )
    # The answer names the module serving it (the authority binding).
    answer = {"tenant_ids": [], "sandloom.authority": "sandloom_tenancy"}
    core.set_service_resolver(lambda name, binding, **_: _Proxy(answer))
    claims = await core.collect_claims(uuid4(), object(), issuer="identity")
    assert claims["roles"] == ["member"]
    assert claims["tenant_ids"] == []
    assert claims["claims_sources"] == ["identity", "tenancy"]
    assert not await _is_platform_admin(claims)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", sorted(RESERVED_CLAIM_KEYS))
async def test_no_owner_writes_a_reserved_key(key: str) -> None:
    """The kernel's and the envelope's keys: nobody's, the authority's neither."""
    core = _core()

    async def sneaky(user_id: Any) -> dict[str, Any]:
        """An optional owner trying to write a reserved key."""
        return {"roles": ["admin"], key: 9_999_999_999_000_000}

    with core.module_registration("sandloom_identity") as api:
        api.add_claims_contributor(sneaky)
    claims = await core.collect_claims(uuid4(), object(), issuer="identity")
    assert "roles" not in claims
    assert claims.get(key) != 9_999_999_999_000_000
    assert claims["claims_sources_failed"] == ["identity"]
    assert not await _is_platform_admin(claims)


@pytest.mark.asyncio
async def test_a_critical_owner_writing_a_reserved_key_refuses_the_mint() -> None:
    """The same with a critical owner: 503 naming it."""
    core = _core()

    async def sneaky(user_id: Any) -> dict[str, Any]:
        """A critical owner writing ``iat_us``."""
        return {"roles": ["admin"], "iat_us": 9_999_999_999_000_000}

    with core.module_registration("sandloom_identity") as api:
        api.add_claims_contributor(sneaky, critical=True)
    with pytest.raises(ClaimsUnavailable) as exc_info:
        await core.collect_claims(uuid4(), object(), issuer="identity")
    assert exc_info.value.owner == "identity"


def test_reserved_keys_cover_the_envelope_and_provenance() -> None:
    """The reserved set covers the token envelope and the provenance keys."""
    assert {
        "claims_sources",
        "claims_sources_failed",
        "iat_us",
        "auth_time",
        "sub",
        "iat",
        "nbf",
        "exp",
        "jti",
        "iss",
        "aud",
        "token_type",
        "user_id",
        "provenance_authoritative",
        "claims",
        "tenant_id",
        "org_id",
    } <= RESERVED_CLAIM_KEYS
