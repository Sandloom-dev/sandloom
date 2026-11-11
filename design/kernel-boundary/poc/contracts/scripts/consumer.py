"""kb-poc-contracts: billing as a consumer-only process (sandloom +
sandloom-billing wheels, no sandloom-tenancy) bound to a remote
``tenancy.membership.v1``.

    <venv>/bin/python -I consumer.py --keys keys.json --provider-port 39701 \
        --compat strict [--call count_members]

It composes the real app (``import sandloom.main``), then runs the two halves
of the compatibility guard the app runs: the startup task body
(``check_remote_contract_compat``, lifecycle.py) and the ``/ready`` services
check (``check_remote_service_bindings``, ops.py).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata as md
import importlib.util
import json
import logging
import os
import uuid

parser = argparse.ArgumentParser()
parser.add_argument("--keys", required=True)
parser.add_argument("--provider-port", type=int, required=True)
parser.add_argument("--compat", default="strict")
parser.add_argument("--call", action="append", default=[])
parser.add_argument("--required", default="true")
args = parser.parse_args()
os.environ.update(json.load(open(args.keys)))  # noqa: SIM115
os.environ.update(
    {
        "SANDLOOM_MODULES": '["sandloom_billing"]',
        "CONTRACT_COMPAT_CHECK": args.compat,
        "SERVICE_BINDINGS": json.dumps(
            {
                "tenancy.membership.v1": {
                    "base_url": f"http://127.0.0.1:{args.provider_port}",
                    "service_id": "poc",
                    "required": args.required == "true",
                }
            }
        ),
    }
)
logging.basicConfig(level=logging.WARNING, format="[%(levelname)s] %(name)s: %(message)s")
for noisy in ("sandloom.app", "sandloom.core.config", "sandloom.settings"):
    logging.getLogger(noisy).setLevel(logging.ERROR)

ORG = uuid.UUID("a6c056c8-df19-42c8-adf2-153e502a5112")
USER = uuid.UUID("1a862ade-dbb1-49aa-8b5f-9073ed599eda")


async def main() -> None:
    dists = sorted(
        f"{d.metadata['Name']}=={d.version}"
        for d in md.distributions()
        if d.metadata["Name"].startswith("sandloom")
    )
    print("installed:", dists)
    print("find_spec('sandloom_tenancy'):", importlib.util.find_spec("sandloom_tenancy"))
    shared = importlib.util.find_spec("sandloom.shared.contracts")
    print("find_spec('sandloom.shared.contracts'):", shared)

    import sandloom.main  # noqa: F401  (composes the app)
    from sandloom.core.rpc.readiness import (
        ContractIncompatibleError,
        check_remote_contract_compat,
        check_remote_service_bindings,
    )
    from sandloom.sdk import Contract
    try:  # candidate (B1): billing's own copy
        from sandloom_billing._generated.contracts import tenancy_dto
        from sandloom_billing._generated.contracts.tenancy_membership_v1 import (
            TENANCY_MEMBERSHIP_V1,
        )
    except ImportError:  # base: the kernel wheel's copy
        from sandloom.shared.contracts import tenancy_dto  # type: ignore[no-redef]
        from sandloom.shared.contracts.tenancy_membership_v1 import (  # type: ignore[no-redef]
            TENANCY_MEMBERSHIP_V1,
        )

    core = sandloom.main.app.state.core
    print("loaded_modules:", list(core.loaded_modules))
    client = Contract(TENANCY_MEMBERSHIP_V1, optional=True).client()
    print("client:", type(client).__name__)
    state = await client.get_member_state(ORG, USER)
    cls = type(state)
    print(f"get_member_state -> {cls.__module__}.{cls.__qualname__}: {state!r}")
    print("is billing's own copy:", cls is tenancy_dto.MemberState)
    # billing's own path: the hand-written name (services/tenant_resolver.py)
    from sandloom_billing.services import tenant_resolver as tr

    provider = core.get_service("tenancy.membership.v1")
    raw = await provider.get_member_state(ORG, USER)
    print(f"core.get_service(name).get_member_state -> {type(raw).__module__}.{type(raw).__qualname__}")
    via = await tr._member_state(provider, ORG, USER)
    print(f"billing tenant_resolver._member_state -> {type(via).__module__}.{type(via).__qualname__} version={via.version}")
    for name in args.call:
        try:
            value = await getattr(client, name)(ORG)
            print(f"call {name} -> {value!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"call {name} -> {type(exc).__name__}: {exc}")
    from sandloom.core import contract_compat

    found = contract_compat.expected_contracts(core, ["tenancy.membership.v1"])
    for name, exp in found.items():
        print(f"expectation of {name}: {exp.source.split('site-packages/')[-1]}")
    if not found:
        print("expectation of tenancy.membership.v1: none")
    try:
        startup = await check_remote_contract_compat(core)
        print(f"startup compat ({args.compat}): {startup.status} {json.dumps(startup.details, default=str)}")
    except ContractIncompatibleError as exc:
        print(f"startup compat ({args.compat}): REFUSED ContractIncompatibleError: {exc}")
    ready = await check_remote_service_bindings(core)
    print(f"/ready services: {ready.status} {json.dumps(ready.details, default=str)}")


asyncio.run(main())
