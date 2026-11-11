"""The end of the compatibility window at the gates (ADR-0014 decision 4,
"Test of the window end"; G-RISK-14).

Real signed tokens, real gates: ``Role("admin")``, ``Perm(...)``,
``require_platform_admin``, the console's ``platform_operator``,
``TenantScope`` on a forged tenant, ``Role("admin") | AlwaysAllow`` and
``~Role("admin")``, mono and split, with ``legacy_tokens`` still written in
the verifier's composition.

The tokens have no ``cpv`` (the release before KB-11): the gate probe G2-G4
shape (``roles: ["admin"]`` and a tenant grant the user does not hold,
written by a non-authority contributor while the authority ran and omitted
them: the authority is in ``claims_sources``), and the same claims with the
authority absent from ``claims_sources``.

The clock is controlled for the trusted view only: the signature and
``exp`` are checked by PyJWT at the real time, so the token is valid and
what refuses it at the window end is the view, not its expiry. (A real
token issued before ``until`` with ``exp - iat`` within the lifetime is
expired by ``until + lifetime + leeway`` anyway: the view's bound is the
second wall, tested here on its own.)
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from sandloom.core.admin.api.platform import platform_operator
from sandloom.core.auth import claims_view as view
from sandloom.core.auth import tokens
from sandloom.core.config.settings import settings
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
LIFETIME = settings.access_token_expire_minutes * 60
LEEWAY = settings.jwt_leeway_seconds


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
#: Inside the window, the G2-G4 token's values are what the accepted risk
#: keeps open: every gate reads them (``~Role`` denies an admin).
INSIDE = {**dict.fromkeys(GATES, 200), "/not-role": 403}
#: At and after the window end no token without ``cpv`` gives rights: a 401
#: a refresh fixes, ``A | AlwaysAllow`` still allows, ``~A`` never does.
ENDED = {**dict.fromkeys(GATES, 401), "/role-or-always": 200}
#: The authority absent from ``claims_sources``: a definite deny inside the
#: window (the source check), the grant still read.
SOURCELESS = {
    **dict.fromkeys(("/role", "/perm", "/admin", "/operator"), 403),
    "/tenant": 200,
    "/role-or-always": 200,
    "/not-role": 200,
}


@pytest.fixture(autouse=True)
def _uncached_views(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every request verifies and views its token afresh (the cache would
    re-check ``exp`` at the controlled clock)."""
    tokens._verified_tokens.clear()
    monkeypatch.setattr(tokens, "_remember_verified", lambda *a, **k: None)
    yield
    tokens._verified_tokens.clear()


class _ViewClock:
    """``tokens.time`` with ``time()`` at a controlled instant: the trusted
    view's ``now`` (PyJWT keeps the real clock)."""

    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


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


def _verifier(placement: str, until: float) -> CoreFacade:
    """``legacy_tokens`` still written: the monolith runs both authorities'
    short names loaded, the split service neither."""
    loaded = {"identity": ID} if placement == "mono" else None
    return _matrix.verifier(_matrix.roles_map(ID), legacy_until=until, loaded=loaded)


async def _g2_g4(old: Any, user: UUID, *, sources: list[str] | None = None) -> str:
    """The G2-G4 shape minted by an O issuer: ``roles`` and a foreign tenant
    grant and binding a non-authority wrote while the authority ran and
    omitted them (the authority stays in ``claims_sources``)."""
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
async def test_inside_the_window_the_accepted_risk_reads_and_the_source_check_holds(
    issuers: Callable[..., Any], placement: str
) -> None:
    """(a) Inside the window the G2-G4 token's values read (the accepted
    risk, counted); without its authority in ``claims_sources`` the same
    claims give no rights."""
    old = issuers(ID, old=True)
    user = uuid4()
    core = _verifier(placement, until=time.time() + 3600)
    assert await _statuses(core, await _g2_g4(old, user)) == INSIDE
    assert core.get_metric(view.legacy_metric_name("roles"))
    sourceless = await _g2_g4(old, user, sources=["grants"])
    assert await _statuses(core, sourceless) == SOURCELESS


@pytest.mark.asyncio
@pytest.mark.parametrize("placement", ["mono", "split"])
@pytest.mark.parametrize("after", [0.0, 1.0, 86_400.0], ids=["at", "after", "a-day"])
async def test_at_and_after_the_window_end_no_token_without_cpv_gives_rights(
    issuers: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    placement: str,
    after: float,
) -> None:
    """(c) A token issued before ``until``, still valid by its signature
    and ``exp``, read at ``until + lifetime + leeway`` or later: every gate
    refuses with 401 ``token_policy_outdated``, ``Role | AlwaysAllow``
    allows, ``~Role`` never allows. The control (one second before the end)
    reads it."""
    old = issuers(ID, old=True)
    user = uuid4()
    until = time.time() + 60  # the token below is issued before it
    token = await _g2_g4(old, user)
    core = _verifier(placement, until)
    window_end = until + LIFETIME + LEEWAY
    monkeypatch.setattr(tokens, "time", _ViewClock(window_end - 1))
    assert await _statuses(core, token) == INSIDE
    monkeypatch.setattr(tokens, "time", _ViewClock(window_end + after))
    assert await _statuses(core, token) == ENDED
    for gate in ("/role", "/perm", "/admin", "/operator", "/tenant", "/not-role"):
        assert _outdated(await _code(core, token, gate)), gate
    state = view.legacy_tokens_state(
        core.claim_authorities,
        now=window_end + after,
        lifetime_seconds=LIFETIME,
        leeway_seconds=LEEWAY,
    )
    assert state["mode"] == view.LEGACY_ENDED  # diagnostics show it off
