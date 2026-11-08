"""kb-poc-contracts proof 8: two copies of one DTO and two contract versions
in one consumer process, over RPC.

    python run.py provider <keys.json> <port>     # imports acme
    python run.py consumer <keys.json> <port>     # never imports acme
"""

import asyncio
import json
import os
import sys

mode, keys, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
os.environ.update(json.load(open(keys)))  # noqa: SIM115
os.environ["SANDLOOM_MODULES"] = "[]"
os.environ["RPC_ALLOWED_CALLS"] = json.dumps({"poc": ["acme.quota.v1", "acme.quota.v2"]})
os.environ["SERVICE_BINDINGS"] = json.dumps(
    {
        name: {"base_url": f"http://127.0.0.1:{port}", "service_id": "poc"}
        for name in ("acme.quota.v1", "acme.quota.v2")
    }
)


class _S:
    def __init__(self):
        self.info = {}


class _U:
    session = None

    async def __aenter__(self):
        self.session = _S()
        return self

    async def __aexit__(self, *a):
        self.session = None


class _D:
    def uow_factory(self):
        return _U

    def outbox(self):
        return None

    def audit(self):
        return None


async def provider() -> None:
    import uvicorn
    from fastapi import FastAPI

    from acme.contracts import QuotaV1Contract, QuotaV2Contract
    from sandloom.core.facade import CoreFacade
    from sandloom.core.rpc import install_service_rpc_routes

    app = FastAPI()
    core = CoreFacade(app=app)
    app.state.core = core
    core.database = lambda name="default": _D()
    core.register_service("acme.quota.v1", QuotaV1Contract)
    core.register_service("acme.quota.v2", QuotaV2Contract)
    install_service_rpc_routes(app, core)
    print("provider up", flush=True)
    await uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    ).serve()


async def consumer() -> None:
    import importlib.util

    print("find_spec('acme'):", importlib.util.find_spec("acme"))
    import sandloom.main  # noqa: F401
    from sandloom.core.descriptors.contract_refs import dto_copies
    from sandloom.sdk import Contract
    from sandloom.sdk.contracts import ContractRef

    # the module distribution's copies first, then the application's
    from shopmod._generated.contracts import acme_dto as shop_dto
    from shopmod._generated.contracts.acme_quota_v1 import ACME_QUOTA_V1 as SHOP_V1
    from shopmod._generated.contracts.acme_quota_v2 import ACME_QUOTA_V2 as SHOP_V2
    from app.shared.contracts import acme_dto as app_dto
    from app.shared.contracts.acme_quota_v1 import ACME_QUOTA_V1 as APP_V1

    def name(obj):
        return f"{type(obj).__module__}.{type(obj).__qualname__}"

    print("copies of acme.schemas:Quota loaded:", [f"{c.__module__}.{c.__qualname__}" for c in dto_copies("acme.schemas:Quota")])
    print("copies of acme.schemas:Limit loaded:", [f"{c.__module__}.{c.__qualname__}" for c in dto_copies("acme.schemas:Limit")])
    v1 = await Contract(SHOP_V1).read("a")
    v2 = await Contract(SHOP_V2).read("a")
    app1 = await Contract(APP_V1).read("a")
    print("shopmod v1 typed ->", name(v1), "| limit:", name(v1.limit))
    print("shopmod v2 typed ->", name(v2), "| limits[0]:", name(v2.limits[0]))
    print("app v1 typed     ->", name(app1), "| limit:", name(app1.limit))
    print("shopmod v1 is shop copy:", type(v1) is shop_dto.Quota, "| app v1 is app copy:", type(app1) is app_dto.Quota)
    print("v1 Limit and v2 Limit are one class:", type(v1.limit) is type(v2.limits[0]))
    hand = await Contract(ContractRef("acme.quota.v1")).read("a")
    print("hand-written ContractRef('acme.quota.v1') ->", name(hand))


asyncio.run(provider() if mode == "provider" else consumer())
