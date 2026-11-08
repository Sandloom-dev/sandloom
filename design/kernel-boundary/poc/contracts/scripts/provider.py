"""kb-poc-contracts: a DB-free provider of ``tenancy.membership.v1`` served
by the kernel's own RPC routes (pattern of tools/packaging/wheel_rpc_smoke.py).

Run with a provider venv's interpreter, isolated::

    <venv>/bin/python -I provider.py --keys keys.json --port 39701 --variant N

Variants (the DTO is the INSTALLED tenancy wheel's ``MemberState`` unless
``acme``):

- ``N``       the six methods of tenancy N;
- ``N1``      tenancy N+1: the six methods + ``count_members`` (additive);
              ``MemberState`` comes from the N+1 wheel (extra optional field);
- ``missing`` tenancy N without ``get_role`` (a declared method gone);
- ``acme``    a replacement ``acme_orgs`` (no sandloom-tenancy installed): its
              own ``acme_orgs.schemas:MemberState`` with the same fields;
- ``acme-missing`` acme_orgs without ``get_role``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import types
import uuid
from typing import Any

parser = argparse.ArgumentParser()
parser.add_argument("--keys", required=True)
parser.add_argument("--port", type=int, required=True)
parser.add_argument("--variant", required=True)
args = parser.parse_args()
os.environ.update(json.load(open(args.keys)))  # noqa: SIM115
os.environ["SANDLOOM_MODULES"] = "[]"

from pydantic import BaseModel, ConfigDict  # noqa: E402

if args.variant.startswith("acme"):
    pkg = types.ModuleType("acme_orgs")
    pkg.__path__ = []  # type: ignore[attr-defined]
    schemas = types.ModuleType("acme_orgs.schemas")
    sys.modules["acme_orgs"] = pkg
    sys.modules["acme_orgs.schemas"] = schemas

    class MemberState(BaseModel):  # the replacement's own DTO, same fields
        model_config = ConfigDict(frozen=True)
        organization_id: uuid.UUID
        user_id: uuid.UUID
        role: str | None
        version: int

    MemberState.__module__ = "acme_orgs.schemas"
    schemas.MemberState = MemberState  # type: ignore[attr-defined]
else:
    from sandloom_tenancy.schemas.membership import MemberState  # type: ignore

from sandloom.core.module_api import contract_method  # noqa: E402

ORG = uuid.UUID("a6c056c8-df19-42c8-adf2-153e502a5112")


def _state(organization_id: uuid.UUID, user_id: uuid.UUID) -> Any:
    extra = {}
    if "display_name" in MemberState.model_fields:
        extra["display_name"] = "Ada"
    return MemberState(
        organization_id=organization_id,
        user_id=user_id,
        role="admin",
        version=7,
        **extra,
    )


class Base:
    @classmethod
    @contract_method
    async def get_member_state(
        cls, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> MemberState:
        return _state(organization_id, user_id)

    @classmethod
    @contract_method
    async def has_role_at_least(
        cls, organization_id: uuid.UUID, user_id: uuid.UUID, minimum: str
    ) -> bool:
        return minimum in ("member", "admin")

    @classmethod
    @contract_method
    async def is_member(cls, organization_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        return True

    @classmethod
    @contract_method
    async def list_member_user_ids(
        cls, organization_id: uuid.UUID, after: uuid.UUID | None, limit: int
    ) -> list[uuid.UUID]:
        return []

    @classmethod
    @contract_method
    async def list_user_organization_ids(cls, user_id: uuid.UUID) -> list[uuid.UUID]:
        return [ORG]


class WithRole(Base):
    @classmethod
    @contract_method
    async def get_role(cls, organization_id: uuid.UUID, user_id: uuid.UUID) -> str | None:
        return "admin"


class Next(WithRole):
    @classmethod
    @contract_method
    async def count_members(cls, organization_id: uuid.UUID) -> int:
        return 3


IMPL = {
    "N": WithRole,
    "N1": Next,
    "missing": Base,
    "acme": WithRole,
    "acme-missing": Base,
}[args.variant]


class _Session:
    def __init__(self) -> None:
        self.info: dict[str, Any] = {}


class _UoW:
    session: Any = None

    async def __aenter__(self) -> _UoW:
        self.session = _Session()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.session = None


class _Database:
    def uow_factory(self) -> Any:
        return _UoW

    def outbox(self) -> None:
        return None

    def audit(self) -> None:
        return None


def app() -> Any:
    from fastapi import FastAPI

    from sandloom.core.facade import CoreFacade
    from sandloom.core.module_api import contract_schema
    from sandloom.core.rpc import install_service_rpc_routes

    application = FastAPI()
    core = CoreFacade(app=application)
    application.state.core = core
    core.database = lambda name="default": _Database()  # type: ignore[method-assign]
    core.register_service("tenancy.membership.v1", IMPL)
    install_service_rpc_routes(application, core)
    schema = contract_schema(IMPL)
    print(
        f"provider {args.variant}: methods {sorted(schema.methods)}; "
        f"MemberState {MemberState.__module__}:{MemberState.__qualname__} "
        f"fields {sorted(MemberState.model_fields)}; fingerprint {schema.hash}",
        flush=True,
    )
    return application


async def main() -> None:
    import uvicorn

    config = uvicorn.Config(
        app(), host="127.0.0.1", port=args.port, log_level="warning", lifespan="off"
    )
    await uvicorn.Server(config).serve()


asyncio.run(main())
