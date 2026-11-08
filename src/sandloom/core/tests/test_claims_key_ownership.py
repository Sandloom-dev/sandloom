"""A token claim has one owner: no contributor writes another's claim.

``require_platform_admin`` and the platform console's ``platform_operator``
trust ``roles`` when ``"identity"`` is in ``claims_sources``. That provenance
names the owners that RAN, not the owner of each key: before this check the
merge was a plain ``dict.update`` in plan order (local owners in registration
order, then every remote ``<owner>.claims.v1`` binding), so any composed module
returning ``{"roles": ["admin"]}`` replaced identity's roles in a token still
stamped ``claims_sources: ["identity", ...]``: platform administrator. The
same held for the token envelope a contributor could pre-fill (``iat_us`` past
every revocation mark, ``auth_time`` past step-up, ``jti`` / ``iss`` / ``aud``,
which ``create_access_token`` only ``setdefault``\\ s).

Now the merge refuses minting (503, fail closed) when a second owner writes a
key an earlier owner wrote, or when any owner writes a key the kernel or the
token envelope stamps. One owner's own contributors still merge freely, and
identity's tokens are unchanged.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException

from sandloom.core.facade import ClaimsUnavailable, CoreFacade
from sandloom.core.facade._claims import RESERVED_CLAIM_KEYS
from sandloom.shared.guards.platform import require_platform_admin
from sandloom.shared.subject import UserContext


class _FakeSession:
    def __init__(self) -> None:
        self.info: dict[str, Any] = {}


class _FakeUoW:
    def __init__(self) -> None:
        self.session = _FakeSession()

    async def __aenter__(self) -> _FakeUoW:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeDatabase:
    def uow_factory(self) -> Any:
        return _FakeUoW

    def outbox(self) -> None:
        return None

    def audit(self) -> None:
        return None


def _core() -> CoreFacade:
    core = CoreFacade(app=FastAPI())
    core.database = lambda name="default": _FakeDatabase()  # type: ignore[method-assign]
    return core


class _Proxy:
    """A remote ``<owner>.claims.v1`` client (what RPC would return)."""

    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result

    async def contribute(self, user_id: Any) -> dict[str, Any]:
        return self._result


async def _identity(user_id: Any) -> dict[str, Any]:
    return {"roles": ["member"], "permissions": []}


def _with_identity(core: CoreFacade) -> None:
    with core.module_registration("sandloom_identity") as api:
        api.add_claims_contributor(_identity, critical=True)


async def _is_platform_admin(claims: dict[str, Any]) -> bool:
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
        return {"roles": ["member"]}

    async def second(user_id: Any) -> dict[str, Any]:
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

    Before the check this token carried ``roles == ["admin"]`` with
    ``claims_sources == ["identity", "rogue"]`` and passed the platform gate.
    """
    core = _core()
    _with_identity(core)

    async def rogue(user_id: Any) -> dict[str, Any]:
        return {key: ["admin"]}

    with core.module_registration("acme.rogue") as api:
        api.add_claims_contributor(rogue)  # not even critical
    with pytest.raises(ClaimsUnavailable) as exc_info:
        await core.collect_claims(uuid4(), object(), issuer="identity")
    assert exc_info.value.status_code == 503
    assert exc_info.value.owner == "rogue"


@pytest.mark.asyncio
async def test_monolith_order_does_not_matter() -> None:
    """A module registered BEFORE identity is refused too (no silent loser)."""
    core = _core()

    async def rogue(user_id: Any) -> dict[str, Any]:
        return {"roles": ["admin"]}

    with core.module_registration("acme.rogue") as api:
        api.add_claims_contributor(rogue)
    _with_identity(core)
    with pytest.raises(ClaimsUnavailable) as exc_info:
        await core.collect_claims(uuid4(), object(), issuer="identity")
    assert exc_info.value.owner == "identity"


@pytest.mark.asyncio
async def test_split_remote_owner_cannot_write_identitys_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Split deploy: identity mints alone; a ``rogue.claims.v1`` binding answers
    ``roles: [admin]`` over RPC.

    Remote owners run after every local one, so before the check the remote
    answer always won, and the identity-signed token was accepted by every
    service's platform gate.
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
    with pytest.raises(ClaimsUnavailable) as exc_info:
        await core.collect_claims(uuid4(), object(), issuer="identity")
    assert exc_info.value.owner == "rogue"


@pytest.mark.asyncio
async def test_split_remote_owner_with_its_own_keys_still_contributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = _core()
    _with_identity(core)
    monkeypatch.setattr(
        core.settings,
        "service_bindings",
        {"tenancy.claims.v1": {"base_url": "http://tenancy"}},
    )
    core.set_service_resolver(lambda name, binding, **_: _Proxy({"tenant_ids": []}))
    claims = await core.collect_claims(uuid4(), object(), issuer="identity")
    assert claims["roles"] == ["member"]
    assert claims["tenant_ids"] == []
    assert claims["claims_sources"] == ["identity", "tenancy"]
    assert not await _is_platform_admin(claims)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", sorted(RESERVED_CLAIM_KEYS))
async def test_no_owner_writes_a_reserved_key(key: str) -> None:
    """The kernel's and the envelope's keys: refused for every owner."""
    core = _core()

    async def sneaky(user_id: Any) -> dict[str, Any]:
        return {key: 9_999_999_999_000_000}

    with core.module_registration("sandloom_identity") as api:
        api.add_claims_contributor(sneaky)
    with pytest.raises(ClaimsUnavailable) as exc_info:
        await core.collect_claims(uuid4(), object(), issuer="identity")
    assert exc_info.value.owner == "identity"


def test_reserved_keys_cover_the_envelope_and_provenance() -> None:
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
    } <= RESERVED_CLAIM_KEYS
