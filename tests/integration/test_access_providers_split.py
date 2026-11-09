"""Access-bearing contract providers, monolith and split (ADR-0014 decision 6).

A third-party module ``acme_orgs`` serves ``tenancy.membership.v1`` (the
contract ``OrgRole`` asks through the default membership adapter). Two
in-process apps wired like a split deployment, over the real internal RPC
(proxy -> ``/_sandloom/rpc`` -> contract runtime, ASGI transport):

- **orgs** — loads ``acme_orgs`` under the composition root's assignment
  (``access_providers``) and serves the RPC routes; without the assignment the
  provider process refuses ``acme_orgs``'s registration (SANDLOOM-B034);
- **api** — loads no provider and binds ``tenancy.membership.v1`` through
  ``SERVICE_BINDINGS``, which is the remote assignment: only the pinned
  ``service_id`` answers (pair-MAC authenticated). A process holding other
  keys behind the same URL never decides access.

``OrgRole`` decides the same in the provider process (in-process call) and in
the consumer (RPC).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from sandloom.core import rpc
from sandloom.core.app_factory import access_providers as _access
from sandloom.core.contract_testing import rpc_settings
from sandloom.core.facade import CoreFacade
from sandloom.core.facade._services import ForeignOwnerRegistration
from sandloom.core.module_api import contract_method
from sandloom.core.rpc import proxy as rpc_proxy
from sandloom.core.rpc.errors import RemoteServiceUnavailable
from sandloom.shared.authz import OrgRole
from sandloom.shared.authz import live_contracts as _live
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.authz.results import AccessDecision
from sandloom.shared.subject import UserContext

pytestmark = pytest.mark.asyncio

LOG = logging.getLogger("test.access_providers_split")
MEMBERSHIP = "tenancy.membership.v1"
USER = uuid4()
ORG = uuid4()
OTHER_ORG = uuid4()
ROLES = {"member": 0, "admin": 1, "owner": 2}


class _AcmeMembership:
    """``acme_orgs``'s implementation of the membership contract."""

    @classmethod
    @contract_method
    async def has_role_at_least(cls, org_id: UUID, user_id: UUID, minimum: str) -> bool:
        role = "admin" if (org_id, user_id) == (ORG, USER) else None
        return role is not None and ROLES[role] >= ROLES[minimum]


class _FakeUoW:
    session = SimpleNamespace(info={})

    async def __aenter__(self) -> _FakeUoW:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeDatabase:
    def uow_factory(self) -> Callable[[], _FakeUoW]:
        return _FakeUoW

    def outbox(self) -> None:
        return None

    def audit(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _default_adapters() -> Iterator[None]:
    _live.reset_live_adapters()
    yield
    _live.reset_live_adapters()


def _provider(access_providers: dict[str, str] | None) -> tuple[FastAPI, CoreFacade]:
    """The provider process, built in ``create_app``'s order: the root's
    assignment, the module load, the post-load sweep, the RPC routes."""
    app = FastAPI()
    core = CoreFacade(app=app)
    app.state.core = core
    core.database = lambda name="default": _FakeDatabase()  # type: ignore[method-assign]
    _access.install_access_providers(core, access_providers=access_providers, log=LOG)
    with core.module_registration("acme_orgs") as api:
        api.register_service(MEMBERSHIP, _AcmeMembership)
    _access.enforce_access_providers(core, log=LOG)
    rpc.install_service_rpc_routes(app, core)
    return app, core


def _consumer(service_id: str, peers: tuple[str, ...]) -> CoreFacade:
    """The api process: no provider, ``MEMBERSHIP`` bound to ``service_id``."""
    from sandloom.core.config.settings import settings

    app = FastAPI()
    cfg = settings.model_copy(
        update={
            **rpc_settings("api", peers=peers),
            "service_bindings": {
                MEMBERSHIP: {"base_url": "http://orgs", "service_id": service_id}
            },
        }
    )
    core = CoreFacade(app=app, settings_obj=cfg)
    app.state.core = core
    _access.install_access_providers(core, access_providers=None, log=LOG)
    _access.enforce_access_providers(core, log=LOG)

    def resolve(
        service_name: str,
        binding: dict[str, object],
        *,
        default_timeout_seconds: float,
        settings: object = None,
    ) -> rpc.RemoteServiceProxy:
        return rpc.RemoteServiceProxy(
            service_name=service_name,
            binding=binding,
            default_timeout_seconds=default_timeout_seconds,
            settings=cfg,
        )

    core.set_service_resolver(resolve)
    return core


def _serve(
    monkeypatch: pytest.MonkeyPatch, provider_id: str
) -> tuple[CoreFacade, httpx.AsyncClient]:
    """Key the provider process as ``provider_id`` and route RPC to it."""
    from sandloom.core.config.settings import settings

    keys = rpc_settings(
        provider_id, peers=("api",), allowed_calls={"api": [MEMBERSHIP]}
    )
    for name, value in keys.items():
        monkeypatch.setattr(settings, name, value)
    app, core = _provider({MEMBERSHIP: "acme_orgs"})
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://orgs"
    )
    monkeypatch.setattr(rpc_proxy, "_rpc_client", lambda: client)
    return core, client


@pytest.fixture
def split(monkeypatch: pytest.MonkeyPatch) -> dict[str, CoreFacade]:
    provider, _client = _serve(monkeypatch, "orgs")
    return {"provider": provider, "consumer": _consumer("orgs", ("orgs",))}


def _subject() -> UserContext:
    return UserContext.model_validate({"sub": str(USER)})


def _request(org_id: UUID) -> Any:
    return SimpleNamespace(
        path_params={"org_id": str(org_id)}, query_params={}, headers={}
    )


def _key(d: AccessDecision) -> tuple[Any, ...]:
    return (d.allowed, d.http_status, d.category, d.code, d.indeterminate)


async def test_the_provider_process_refuses_an_unassigned_third_party() -> None:
    """Split boot: the ``orgs`` process loads ``acme_orgs`` with no assignment."""
    with pytest.raises(ForeignOwnerRegistration, match="SANDLOOM-B034"):
        _provider(None)


async def test_the_provider_process_refuses_an_app_local_namesake() -> None:
    """X3 in the provider process: ``myapp.modules.acme_orgs`` is not it."""
    core = CoreFacade(app=FastAPI())
    _access.install_access_providers(
        core, access_providers={MEMBERSHIP: "acme_orgs"}, log=LOG
    )
    with (
        pytest.raises(ForeignOwnerRegistration, match="'acme_orgs'"),
        core.module_registration("myapp.modules.acme_orgs") as api,
    ):
        api.register_service(MEMBERSHIP, _AcmeMembership)


@pytest.mark.parametrize(
    ("org_id", "minimum", "allowed"),
    [
        (ORG, "member", True),
        (ORG, "admin", True),
        (ORG, "owner", False),
        (OTHER_ORG, "member", False),
    ],
)
async def test_an_assigned_provider_decides_the_same_mono_and_split(
    split: dict[str, CoreFacade], org_id: UUID, minimum: str, allowed: bool
) -> None:
    policy = OrgRole(minimum)
    mono = await policy.decide(
        AuthzContext(_subject(), core=split["provider"], request=_request(org_id))
    )
    remote = await policy.decide(
        AuthzContext(_subject(), core=split["consumer"], request=_request(org_id))
    )
    assert _key(remote) == _key(mono)
    assert mono.allowed is allowed


async def test_only_the_pinned_service_id_answers_remotely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The binding pins ``orgs``; a process keyed ``impostor`` behind the same
    URL serves an assigned provider, yet its answer is never taken: the call
    is refused (401 at the provider, 503 to the caller), negation included,
    never an allow."""
    _serve(monkeypatch, "impostor")
    consumer = _consumer("orgs", ("orgs", "impostor"))
    for policy in (OrgRole("member"), ~OrgRole("member")):
        ctx = AuthzContext(_subject(), core=consumer, request=_request(ORG))
        with pytest.raises(RemoteServiceUnavailable) as exc:
            await policy.decide(ctx)
        assert exc.value.status_code == 503
