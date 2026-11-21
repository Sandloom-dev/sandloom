"""A token without ``cpv`` at the gates (ADR-0014 decision 4, stage 3, always).

Real signed tokens, real gates: ``Role("admin")``, ``Perm(...)``,
``require_platform_admin``, the console's ``platform_operator``,
``TenantScope`` on a forged tenant, ``Role("admin") | AlwaysAllow`` and
``~Role("admin")``, mono and split. There is no compatibility mode (the
bounded ``legacy_tokens`` mode was removed 2026-11-21): a token without
``cpv`` gives no rights, whatever its ``iat`` and whether or not its
authority ran at the mint.

The tokens have no ``cpv`` (the release before KB-11): the gate probe G2-G4
shape (``roles: ["admin"]`` and a tenant grant the user does not hold,
written by a non-authority contributor while the authority ran and omitted
them: the authority is in ``claims_sources``), and the same claims with the
authority absent from ``claims_sources``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from sandloom.core.admin.api.platform import platform_operator
from sandloom.core.auth import claims_view as view
from sandloom.core.auth import tokens
from sandloom.core.di import TenantScope
from sandloom.core.facade import CoreFacade
from sandloom.core.runtime import use_active_core
from sandloom.core.tests import test_claims_split_matrix as _matrix
from sandloom.core.tests._entry_points_support import installed_entry_points
from sandloom.shared.authz import Perm, Role, require
from sandloom.shared.authz.algebra import Requirement
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.guards.platform import require_platform_admin

# The split matrix's processes: the keyed RPC caller, the in-memory
# revocation cache and the issuer factory (an O issuer: ``old=True``).
caller = _matrix.caller
mem_cache = _matrix.mem_cache
issuers = _matrix.issuers
_close_shared_client = _matrix._close_shared_client

ID = _matrix.ID


@pytest.fixture(autouse=True)
def _standard_entry_points() -> Iterator[None]:
    """``sandloom_identity`` is the module ``identity`` by its entry point,
    installed or not (the split matrix's fixture, which pytest does not share
    through the import)."""
    with installed_entry_points():
        yield


FOREIGN = str(uuid4())  # a tenant the user holds no grant for


class AlwaysAllow(Requirement):
    """A requirement that always holds (the ``A | B`` control)."""

    async def evaluate(self, ctx: AuthzContext) -> bool:
        return True

    def describe(self) -> str:
        return "always"


def _gated_app() -> FastAPI:
    app = FastAPI()
    gates: dict[str, Any] = {
        "/role": require(Role("admin")),
        "/perm": require(Perm("x.read")),
        "/admin": require_platform_admin,
        "/operator": platform_operator,
        "/role-or-always": require(Role("admin") | AlwaysAllow()),
        "/not-role": require(~Role("admin")),
    }
    for path, gate in gates.items():

        async def _ok() -> dict[str, bool]:
            return {"ok": True}

        app.add_api_route(path, _ok, methods=["GET"], dependencies=[Depends(gate)])

    @app.get("/tenant")
    async def _tenant(tenant: Any = TenantScope()) -> dict[str, str]:
        return {"tenant": str(tenant)}

    return app


GATED = _gated_app()
GATES = (
    "/role",
    "/perm",
    "/admin",
    "/operator",
    "/tenant",
    "/role-or-always",
    "/not-role",
)
#: No token without ``cpv`` gives rights: a 401 a refresh fixes,
#: ``A | AlwaysAllow`` still allows, ``~A`` never does.
NO_CPV = {**dict.fromkeys(GATES, 401), "/role-or-always": 200}


@pytest.fixture(autouse=True)
def _uncached_views() -> Iterator[None]:
    """Every request verifies and views its token afresh."""
    tokens._verified_tokens.clear()
    yield
    tokens._verified_tokens.clear()


async def _statuses(core: CoreFacade, token: str) -> dict[str, int]:
    out: dict[str, int] = {}
    with use_active_core(core):
        async with AsyncClient(
            transport=ASGITransport(app=GATED), base_url="http://verifier"
        ) as client:
            headers = {"Authorization": f"Bearer {token}"}
            for gate in GATES:
                out[gate] = (await client.get(gate, headers=headers)).status_code
    return out


async def _code(core: CoreFacade, token: str, gate: str) -> Any:
    with use_active_core(core):
        async with AsyncClient(
            transport=ASGITransport(app=GATED), base_url="http://verifier"
        ) as client:
            response = await client.get(
                gate, headers={"Authorization": f"Bearer {token}"}
            )
    detail = response.json().get("detail")
    return detail.get("code") if isinstance(detail, dict) else detail


def _outdated(detail: Any) -> bool:
    """The view's refusal (``token_policy_outdated``: the kernel gates name
    the code, an access requirement its reason), never an expired or
    invalid token."""
    return detail == view.OUTDATED or "predates the claim policy" in str(detail)


def _verifier(placement: str) -> CoreFacade:
    """The monolith runs the authorities' short names loaded, the split
    service neither."""
    loaded = {"identity": ID} if placement == "mono" else None
    return _matrix.verifier(_matrix.roles_map(ID), loaded=loaded)


async def _g2_g4(old: Any, user: UUID, *, sources: list[str] | None = None) -> str:
    """The G2-G4 shape minted by an O issuer: ``roles`` and a foreign tenant
    grant and binding a non-authority wrote while the authority ran and
    omitted them (the authority stays in ``claims_sources`` unless
    ``sources`` says otherwise)."""
    forged: dict[str, Any] = {
        "roles": ["admin"],
        "tenant_ids": [FOREIGN],
        "tenant_id": FOREIGN,
    }
    if sources is not None:
        forged["claims_sources"] = sources
    return str(await old.mint(user, **forged))


@pytest.mark.asyncio
@pytest.mark.parametrize("placement", ["mono", "split"])
@pytest.mark.parametrize(
    "sources", [None, ["grants"]], ids=["authority-ran", "authority-absent"]
)
async def test_no_token_without_cpv_gives_rights_at_any_gate(
    issuers: Callable[..., Any], placement: str, sources: list[str] | None
) -> None:
    """The G2-G4 token (forged ``roles`` / tenant grant, no ``cpv``), with or
    without its authority in ``claims_sources``: every gate refuses with 401
    ``token_policy_outdated``, ``Role | AlwaysAllow`` allows, ``~Role`` never
    allows."""
    old = issuers(ID, old=True)
    user = uuid4()
    token = await _g2_g4(old, user, sources=sources)
    core = _verifier(placement)
    assert await _statuses(core, token) == NO_CPV
    for gate in ("/role", "/perm", "/admin", "/operator", "/tenant", "/not-role"):
        assert _outdated(await _code(core, token, gate)), gate
