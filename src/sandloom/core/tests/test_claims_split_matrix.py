"""The claim policy across processes: issuer x verifier (ADR-0014 decision 4).

Split shape, one kernel process per role: an **issuer** facade mints real
signed access tokens (its ``roles`` / ``permissions`` authority loaded, the
``tenant_ids`` authority a separate provider service reached over the keyed
internal RPC, which answers with its authority binding); a **verifier**
facade with its own ``claim_authorities`` map (a service running neither
authority) reads each token through ``decode_token`` and the kernel gates:
``Role("admin")``, ``Perm("x.read")``, ``require_platform_admin``, the
console's ``platform_operator`` and ``TenantScope``.

* the identity -> acme_identity key change, steps 0-3 and rollback a-c, with
  a previous-release token (the current merge without ``cpv``, the only wire
  difference of the release before KB-11; built from that release's wheels
  only by the trust PoC: ADR-0014, amendment "W2+W3 review fixes"),
  the negative rows at step 2, and the change without ``previous=`` (either
  order answers 503 ``claims_policy_mismatch``, never a 401 a refresh cannot
  fix);
* the one-service sequence (issuer and verifier in one process);
* the initial enablement, stages 1-3 and rollback, split and mono with mixed
  old / new replicas (``legacy_tokens``, G-RISK-14);
* every client refreshes at most once per step change.

The monolith rows of the same matrix are ``test_claims_view.py`` (the view
over payloads); the five owner checks are ``test_claims_cpv_identity.py``.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from sandloom.core import rpc
from sandloom.core.admin.api.platform import platform_operator
from sandloom.core.app_factory.claim_authorities import (
    build_claim_authorities,
    filter_to_composition,
)
from sandloom.core.auth import claims_view as view
from sandloom.core.auth import tokens
from sandloom.core.claims import Authority, ClaimAuthorities
from sandloom.core.config.settings import settings
from sandloom.core.di import TenantScope
from sandloom.core.facade import CoreFacade
from sandloom.core.rpc import RemoteServiceProxy
from sandloom.core.runtime import use_active_core
from sandloom.core.testing.auth import sealed_claims
from sandloom.core.tests import test_auth_rotation_revocation as _revocation
from sandloom.core.tests import test_rpc_keyed_binding as _keyed
from sandloom.core.tests._entry_points_support import installed_entry_points
from sandloom.shared.authz import Perm, Role, require
from sandloom.shared.guards.platform import require_platform_admin

caller = _keyed.caller
mem_cache = _revocation.mem_cache

ID = "sandloom_identity"
ACME = "acme.modules.acme_identity"
APP_IDENTITY = "app.modules.identity"  # another module under the short name
GRANTS = "acme.grants"
CONTRACT = "grants.claims.v1"
SHORT = {ID: "identity", ACME: "acme_identity", APP_IDENTITY: "identity"}
TENANT = str(uuid4())
BINDINGS = {CONTRACT: {"base_url": "http://grants", "service_id": "tenancy"}}
LIFETIME = settings.access_token_expire_minutes * 60


@pytest.fixture(autouse=True)
def _standard_entry_points() -> Iterator[None]:
    """``sandloom_identity`` / ``sandloom_tenancy`` are the modules
    ``identity`` / ``tenancy`` by their entry points, installed or not."""
    with installed_entry_points():
        yield


GATES = ("/role", "/perm", "/admin", "/operator", "/tenant")
OK = dict.fromkeys(GATES, 200)
#: ``roles`` / ``permissions`` untrusted for a reason; the grant stays.
ROLES_401 = {**dict.fromkeys(GATES[:4], 401), "/tenant": 200}
ROLES_503 = {**dict.fromkeys(GATES[:4], 503), "/tenant": 200}
ROLES_403 = {**dict.fromkeys(GATES[:4], 403), "/tenant": 200}
#: Every protected key untrusted: a token without ``cpv``, no window.
ALL_401 = dict.fromkeys(GATES, 401)


def _gated_app() -> FastAPI:
    app = FastAPI()

    @app.get("/role", dependencies=[Depends(require(Role("admin")))])
    async def _role() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/perm", dependencies=[Depends(require(Perm("x.read")))])
    async def _perm() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/admin", dependencies=[Depends(require_platform_admin)])
    async def _admin() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/operator", dependencies=[Depends(platform_operator)])
    async def _operator() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/tenant")
    async def _tenant(tenant: Any = TenantScope()) -> dict[str, str]:
        return {"tenant": str(tenant)}

    return app


GATED = _gated_app()


# --- the processes ------------------------------------------------------------
def roles_map(path: str, previous: str | None = None) -> dict[str, Any]:
    """The root's map: ``path`` holds ``roles`` / ``permissions`` (with one
    ``previous=`` authority during a change), ``acme.grants`` the grant."""
    entry: Any = (
        Authority(path, previous=Authority(SHORT[previous], module=previous))
        if previous
        else path
    )
    return {
        "issuer": path,
        "roles": entry,
        "permissions": entry,
        "tenant_ids": GRANTS,
    }


def _provider() -> FastAPI:
    """The ``tenant_ids`` authority: service ``tenancy`` running ``acme.grants``."""
    cfg = settings.model_copy(
        update={
            "rpc_service_id": "tenancy",
            "rpc_private_key": None,
            "rpc_key_id": None,
            "rpc_dh_private_key": _keyed.DH["tenancy"][0],
            "rpc_dh_key_id": "t1",
            "rpc_allowed_calls": {"billing": [CONTRACT]},
        }
    )
    app = FastAPI()
    core = CoreFacade(app=app, settings_obj=cfg)
    app.state.core = core
    core.database = lambda name="default": _keyed._Database()  # type: ignore[method-assign]

    async def grants(_user_id: Any) -> dict[str, Any]:
        return {"tenant_ids": [TENANT]}

    with core.module_registration(GRANTS) as api:
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


class Issuer:
    """An issuing process: ``module`` loaded (``roles`` / ``permissions``),
    ``tenant_ids`` from the provider service over the keyed RPC.

    ``old=True`` is the release before KB-11: the same merge without ``cpv``.
    """

    def __init__(
        self,
        module: str,
        *,
        mapping: dict[str, Any] | None = None,
        old: bool = False,
        provider: FastAPI,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self.old = old
        self.core = core = CoreFacade(app=FastAPI())
        core.database = lambda name="default": _keyed._Database()  # type: ignore[method-assign]
        core._assign_claim_authorities(
            filter_to_composition(
                build_claim_authorities(mapping or roles_map(module)),
                loaded={SHORT[module]: module},
                bindings=BINDINGS,
            )
        )

        async def roles(_user_id: Any) -> dict[str, Any]:
            return {"roles": ["admin"], "permissions": ["x.read"]}

        with core.module_registration(module) as api:
            api.add_claims_contributor(roles, critical=True)
        monkeypatch.setattr(settings, "service_bindings", BINDINGS)
        core.set_service_resolver(_resolver(provider))

    async def claims(self, user: UUID) -> dict[str, Any]:
        claims = dict(await self.core.collect_claims(user))
        if self.old:
            claims.pop("cpv", None)
        return claims

    async def mint(self, user: UUID, **override: Any) -> str:
        """A real signed token. The current release mints through the sealed
        result; the release before KB-11 (``old``) and a forged variant
        (``override``) through the kernel test authority, which mints the
        stated provenance and ``cpv`` (none for ``old``) as given."""
        envelope = {"sub": str(user), "token_type": "access"}
        if not self.old and not override:
            sealed = await self.core.collect_claims(user)
            return tokens.create_access_token(envelope, claims=sealed)
        claims = {**(await self.claims(user)), **override}
        claims.setdefault("cpv", None)
        return tokens.create_access_token(envelope, claims=sealed_claims(claims))


def verifier(
    mapping: dict[str, Any],
    *,
    deployed_at: float | None = None,
    legacy_until: float | None = None,
    loaded: dict[str, str] | None = None,
) -> CoreFacade:
    """A verifying process (by default a service running neither authority)
    with ``mapping`` taking effect at ``deployed_at``."""
    core = CoreFacade(app=FastAPI())
    policy = filter_to_composition(
        build_claim_authorities(mapping), loaded=loaded or {}, bindings={}
    )
    core._assign_claim_authorities(
        dataclasses.replace(
            policy,
            effective_at=time.time() if deployed_at is None else deployed_at,
            legacy_until=legacy_until,
        )
    )
    return core


async def statuses(core: CoreFacade, token: str) -> dict[str, int]:
    """Each gate's status for ``token`` on the verifier ``core``."""
    out: dict[str, int] = {}
    with use_active_core(core):
        async with AsyncClient(
            transport=ASGITransport(app=GATED), base_url="http://verifier"
        ) as client:
            for gate in GATES:
                response = await client.get(
                    gate, headers={"Authorization": f"Bearer {token}"}
                )
                out[gate] = response.status_code
    return out


async def detail(core: CoreFacade, token: str, gate: str) -> Any:
    with use_active_core(core):
        async with AsyncClient(
            transport=ASGITransport(app=GATED), base_url="http://verifier"
        ) as client:
            response = await client.get(
                gate, headers={"Authorization": f"Bearer {token}"}
            )
    return response.json().get("detail")


class Client:
    """A client holding one token: on a 401 it refreshes once at ``issuer``
    and retries; a second 401 in the same step would be a loop (counted,
    asserted by the tests)."""

    def __init__(self, user: UUID, token: str) -> None:
        self.user = user
        self.token = token
        self.refreshes = 0

    async def call(self, core: CoreFacade, issuer: Issuer, gate: str) -> int:
        refreshed = False
        while True:
            with use_active_core(core):
                async with AsyncClient(
                    transport=ASGITransport(app=GATED), base_url="http://verifier"
                ) as client:
                    response = await client.get(
                        gate, headers={"Authorization": f"Bearer {self.token}"}
                    )
            if response.status_code != 401 or refreshed:
                return response.status_code
            self.token = await issuer.mint(self.user)
            self.refreshes += 1
            refreshed = True


@pytest.fixture(autouse=True)
def _fresh_verification_cache() -> Iterator[None]:
    tokens._verified_tokens.clear()
    yield
    tokens._verified_tokens.clear()


@pytest.fixture(autouse=True)
async def _close_shared_client() -> AsyncIterator[None]:
    yield
    await rpc.close_rpc_client()


@pytest.fixture
def issuers(
    caller: Any, mem_cache: Any, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., Issuer]:
    provider = _provider()  # after ``caller`` set the RPC keys

    def make(module: str, **kwargs: Any) -> Issuer:
        return Issuer(module, provider=provider, monkeypatch=monkeypatch, **kwargs)

    return make


# --- the key change identity -> acme_identity --------------------------------
@pytest.mark.asyncio
async def test_the_key_change_steps_and_rollback_in_split(
    issuers: Callable[..., Issuer],
) -> None:
    """ADR-0014 decision 4's table: T1 (M1 issuer, before step 0 ends), T2
    (M2 issuer, at step 2) and T0 (the previous release's token, no ``cpv``)
    replayed at every step on a verifier running neither authority."""
    m1, m2 = issuers(ID), issuers(ACME)
    user = uuid4()
    t0 = await issuers(ID, old=True).mint(user)
    t1 = await m1.mint(user)

    # step 0: M1 / M1
    step0 = verifier(roles_map(ID))
    assert await statuses(step0, t1) == OK
    assert await statuses(step0, t0) == ALL_401  # the old token: one refresh
    # step 1: M1 issuers / M2 verifiers with previous= identity
    step1 = verifier(roles_map(ACME, previous=ID))
    assert await statuses(step1, t1) == OK
    # step 2: M2 issuers mint T2; both read
    t2 = await m2.mint(user)
    step2 = step1
    assert await statuses(step2, t1) == OK
    assert await statuses(step2, t2) == OK
    # step 3: previous= dropped; T1 needs one refresh (401 only where roles
    # or permissions are needed: the grant's digest did not change)
    step3 = verifier(roles_map(ACME))
    assert await statuses(step3, t1) == ROLES_401
    assert await detail(step3, t1, "/admin") == view.OUTDATED
    assert await statuses(step3, t2) == OK
    client = Client(user, t1)
    assert await client.call(step3, m2, "/role") == 200
    assert client.refreshes == 1
    assert await statuses(step3, client.token) == OK

    # rollback a: M2 issuers / M1 verifiers with previous= acme_identity
    rb_a = verifier(roles_map(ID, previous=ACME))
    assert await statuses(rb_a, t1) == OK
    assert await statuses(rb_a, t2) == OK
    # rollback b: issuers back to M1
    rb_b = rb_a
    t1b = await m1.mint(user)
    assert await statuses(rb_b, t1b) == OK
    assert await statuses(rb_b, t2) == OK
    # rollback c: previous= dropped; T2 needs one refresh at an M1 issuer
    rb_c = verifier(roles_map(ID))
    assert await statuses(rb_c, t1) == OK
    assert await statuses(rb_c, t2) == ROLES_401
    client = Client(user, t2)
    assert await client.call(rb_c, m1, "/admin") == 200
    assert client.refreshes == 1


@pytest.mark.asyncio
async def test_the_negative_rows_at_step_2(issuers: Callable[..., Issuer]) -> None:
    """Untrusted at step 2: a digest under the other authority's source
    (both ways), an app-local ``identity`` with another import path, and
    ``identity`` in ``claims_sources_failed``. Each a real signed token."""
    user = uuid4()
    m1 = issuers(ID)
    m1_claims = await m1.claims(user)
    m2_claims = await issuers(ACME).claims(user)
    step2 = verifier(roles_map(ACME, previous=ID), deployed_at=time.time() - 60)

    def sources(owner: str) -> list[str]:
        return [owner, "grants"]

    swapped = [
        await m1.mint(user, claims_sources=sources("acme_identity")),
        await m1.mint(user, cpv=m2_claims["cpv"], claims_sources=sources("identity")),
        await m1.mint(user, claims_sources_failed=["identity"]),
    ]
    for token in swapped:
        assert await statuses(step2, token) == ROLES_403
    # app.modules.identity under the short name ``identity``: another digest.
    # Minted after the map took effect: issuer and verifier disagree (503),
    # never a 401 a refresh at this issuer would not fix.
    app_local = issuers(APP_IDENTITY)
    app_claims = await app_local.claims(user)
    assert app_claims["claims_sources"] == m1_claims["claims_sources"]
    assert app_claims["cpv"]["k"]["roles"] != m1_claims["cpv"]["k"]["roles"]
    assert await statuses(step2, await app_local.mint(user)) == ROLES_503


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["verifiers-first", "issuers-first"])
async def test_a_key_change_without_previous_answers_503_not_a_refresh_loop(
    issuers: Callable[..., Issuer], order: str
) -> None:
    """Without step 1 the change loops during the rollout in either order:
    the kernel answers 503 ``claims_policy_mismatch`` (logged at ERROR) to a
    token minted after the verifier's map took effect, never a 401 whose
    refresh returns the same digest."""
    user = uuid4()
    deployed = time.time() - 60
    if order == "verifiers-first":
        core, issuer = verifier(roles_map(ACME), deployed_at=deployed), issuers(ID)
    else:
        core, issuer = verifier(roles_map(ID), deployed_at=deployed), issuers(ACME)
    client = Client(user, await issuer.mint(user))
    assert await statuses(core, client.token) == ROLES_503
    assert await detail(core, client.token, "/operator") == view.MISMATCH
    assert await client.call(core, issuer, "/role") == 503
    assert client.refreshes == 0


@pytest.mark.asyncio
async def test_one_service_issuer_and_verifier_never_loops(
    issuers: Callable[..., Issuer],
) -> None:
    """Where one service issues and verifies (identity's own service), step 1
    deploys M1 with ``previous=`` M2, step 2 M2 with ``previous=`` M1, step 3
    drops it: during each rolling deploy every replica accepts both digests,
    so a client whose refresh lands on either replica never loops."""
    user = uuid4()
    m1 = issuers(ID)
    m2 = issuers(ACME, mapping=roles_map(ACME, previous=ID))
    loaded = {"identity": ID}
    step1 = verifier(roles_map(ID, previous=ACME), loaded=loaded)
    step2 = verifier(roles_map(ACME, previous=ID), loaded={"acme_identity": ACME})
    t1, t2 = await m1.mint(user), await m2.mint(user)
    for replica in (step1, step2):
        assert await statuses(replica, t1) == OK
        assert await statuses(replica, t2) == OK
    step3 = verifier(roles_map(ACME), loaded={"acme_identity": ACME})
    client = Client(user, t1)
    assert await client.call(step3, m2, "/perm") == 200
    assert client.refreshes == 1


# --- the initial enablement (legacy_tokens, G-RISK-14) ------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("placement", ["split", "mono"])
async def test_the_initial_enablement_stages_and_rollback(
    issuers: Callable[..., Issuer], placement: str
) -> None:
    """Stages 1 -> 3 and back with the previous release's token. ``mono``:
    one rolling deploy of three replicas, old (O) and new (N) mixed, each
    issuer and verifier; ``split``: a verifier running neither authority."""
    user = uuid4()
    old, new = issuers(ID, old=True), issuers(ID)
    until = time.time() + 3600
    loaded = {"identity": ID} if placement == "mono" else None
    t0 = await old.mint(user)
    # stage 1 (+2 in a monolith: one deploy learns and stamps): N verifiers
    # in the compatibility mode; replicas O, N, N. No 401 for any token.
    stage1 = verifier(roles_map(ID), legacy_until=until, loaded=loaded)
    replicas = [old, new, new] if placement == "mono" else [old, new]
    for issuer in replicas:
        token = await issuer.mint(user)
        assert await statuses(stage1, token) == OK, issuer.old
    assert await statuses(stage1, t0) == OK
    assert stage1.get_metric(view.legacy_metric_name("roles"))
    # The old token without its authority in claims_sources gives no rights.
    sourceless = await old.mint(user, claims_sources=["grants"])
    assert await statuses(stage1, sourceless) == ROLES_403
    # stage 3: legacy_tokens removed; one 401 and one refresh per client.
    stage3 = verifier(roles_map(ID), loaded=loaded)
    client = Client(user, t0)
    for gate in GATES:
        assert await client.call(stage3, new, gate) == 200, gate
    assert client.refreshes == 1
    # rollback 3 -> 2: verifiers redeployed with legacy_tokens: T0 reads again.
    back = verifier(roles_map(ID), legacy_until=until, loaded=loaded)
    assert await statuses(back, t0) == OK
    # 2 -> 1: issuers back to O; the compatibility mode reads both.
    assert await statuses(back, await old.mint(user)) == OK
    assert await statuses(back, await new.mint(user)) == OK


@pytest.mark.asyncio
async def test_the_window_bounds_hold_on_real_tokens(
    issuers: Callable[..., Issuer],
) -> None:
    """A token without ``cpv`` issued at or after ``until``, or living longer
    than ``ACCESS_TOKEN_EXPIRE_MINUTES``, gives no rights, ``legacy_tokens``
    still written; inside the window the same claims read.

    ``until`` in the past is the ``iat >= until`` bound (the token is minted
    now), not the window END (``until + lifetime + leeway``): that one is
    ``test_claims_window_end.py``, the view's clock controlled."""
    user = uuid4()
    old = issuers(ID, old=True)
    now = time.time()
    issued_after_until = verifier(roles_map(ID), legacy_until=now - 5)
    assert await statuses(issued_after_until, await old.mint(user)) == ALL_401
    open_ = verifier(roles_map(ID), legacy_until=now + 3600)
    claims = await old.claims(user)
    long_lived = tokens.create_access_token(
        {"sub": str(user), "token_type": "access"},
        expires_delta=settings.access_token_expire_minutes + 5,
        claims=sealed_claims({**claims, "cpv": None}),
    )
    assert await statuses(open_, long_lived) == ALL_401
    assert await statuses(open_, await old.mint(user)) == OK


def test_the_stage_maps_are_the_root_maps() -> None:
    """The matrix's maps resolve as a root's would (``previous=`` per key)."""
    policy: ClaimAuthorities = build_claim_authorities(roles_map(ACME, previous=ID))
    assert dict(policy.previous) == {"roles": ID, "permissions": ID}
    assert "tenant_ids" not in policy.previous
