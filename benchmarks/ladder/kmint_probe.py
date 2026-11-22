"""The sealed mint of three owners under cachegrind (kb-mint-budget-optimize).

``claims_rungs.py`` (``KMint``, ``KMintC``, ``KMintI``) mints with two owners.
The budget of the sealed mint (ADR-0014 decision 5, the KB-12 exception) is
decided on one mint of the standard shape: identity as the token issuer,
tenancy and billing as local owners, in one process, no database and no network
(fake units of work). This file measures it, on the current kernel and on a
build from before ``precomputed`` / ``issuer_claims`` were removed (``ee26108``),
from the same workload.

Host (docker, image ``sandloom-ladder-cg:3.13`` as ``ladder/cg/run_cg.sh``)::

    python benchmarks/ladder/kmint_probe.py [--tree DIR] [--k 1000] [--nopre] PART...

``--tree`` is a checkout root (its ``src`` is mounted read-only); the default is
this checkout. A part prints ``(Ir(K) - Ir(0)) / K``: Ir per operation, with
``PYTHONHASHSEED=0``, ``gc.freeze()`` after 20 warm-up calls of the part itself.
K = 1000: at K = 200 the run-to-run noise is about 1% (14 k Ir), at 1000 about
750 Ir.

Parts: ``mint`` (``collect_claims`` + ``create_access_token``), ``run`` (the
plan alone), ``local`` (one local owner's step), ``issuer`` (the issuer's step),
and on the current kernel ``resolve`` ``require`` ``seal`` ``merge`` ``plan``
``attempt`` ``collect`` ``create`` ``dictcopy`` ``none``. The default is
``run mint local issuer``.

``--nopre`` runs the base build the way the current kernel does (the issuer's
contributor runs, nothing is precomputed): the equal-workload base. Without it
a base build takes ``precomputed`` and skips that contributor. A base tree is a
``git worktree add <dir> ee26108``; the host driver sees ``precomputed`` in its
``_claims.py`` and gives the container the flat ``CACHE_URL`` / ``BROKER_URL``
that build still reads.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import inspect
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

IMAGE = "sandloom-ladder-cg:3.13"
USER = uuid.UUID(int=0xC1A1)
TENANT = uuid.UUID(int=0x7E4A)
IDENTITY = "ladder.identity"
TENANCY = "ladder.tenancy"
BILLING = "ladder.billing"
ENVELOPE = {"sub": str(USER), "token_type": "access"}
DEFAULT_PARTS = ["run", "mint", "local", "issuer"]
WARMUP = 20


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
    def uow_factory(self) -> Callable[[], _UoW]:
        return _UoW

    def outbox(self) -> None:
        return None

    def audit(self) -> None:
        return None


def _build() -> tuple[Any, Any]:
    """The facade with three owners: identity (the issuer, critical), tenancy
    (critical) and billing (optional); returns it and identity's contributor."""
    from fastapi import FastAPI

    import sandloom.core.schema.registry as registry
    from sandloom.core.app_factory.claim_authorities import (
        build_claim_authorities,
        filter_to_composition,
    )
    from sandloom.core.eventing.event_bus import set_active_event_bus
    from sandloom.core.eventing.memory_bus import InMemoryEventBus
    from sandloom.core.facade import CoreFacade
    from sandloom.core.runtime import set_active_core

    set_active_event_bus(InMemoryEventBus(registry.InMemorySchemaRegistry()))
    core = CoreFacade(app=FastAPI())
    core.database = lambda name="default": _Database()  # type: ignore[method-assign]
    mapping = {
        "issuer": IDENTITY,
        "roles": IDENTITY,
        "permissions": IDENTITY,
        "email": IDENTITY,
        "email_verified": IDENTITY,
        "tenant_ids": TENANCY,
        "subscription_status": BILLING,
        "plan_code": BILLING,
        "features": BILLING,
        "limits": BILLING,
    }
    core._assign_claim_authorities(
        filter_to_composition(
            build_claim_authorities(mapping),
            loaded={"identity": IDENTITY, "tenancy": TENANCY, "billing": BILLING},
            bindings={},
        )
    )

    async def identity_roles(_user_id: Any) -> dict[str, Any]:
        return {
            "roles": ["admin", "member"],
            "permissions": ["x.read", "x.write", "y.read"],
            "email": "user@example.com",
            "email_verified": True,
        }

    async def tenancy_grants(_user_id: Any) -> dict[str, Any]:
        return {"tenant_ids": [str(TENANT)]}

    async def billing_subscription(_user_id: Any) -> dict[str, Any]:
        return {
            "subscription_status": "active",
            "plan_code": "pro",
            "features": ["a", "b"],
            "limits": {"seats": 10},
        }

    with core.module_registration(IDENTITY) as api:
        api.add_claims_contributor(identity_roles, critical=True)
    with core.module_registration(TENANCY) as api:
        api.add_claims_contributor(tenancy_grants, critical=True)
    with core.module_registration(BILLING) as api:
        api.add_claims_contributor(billing_subscription)
    set_active_core(core)
    return core, identity_roles


async def _run_part(part: str, count: int) -> None:
    """The setup, ``WARMUP`` calls of ``part`` itself, then ``count`` measured
    ones (``count`` = 0: the common baseline of every part)."""
    import sandloom.core.facade._claims as fc
    from sandloom.core.auth import tokens

    core, identity_fn = _build()
    session = _Session()
    base_build = "precomputed" in inspect.signature(core.collect_claims).parameters
    pre = (
        None
        if os.environ.get("PROBE_NOPRE")
        else {
            identity_fn: {
                "roles": ["admin", "member"],
                "permissions": ["x.read", "x.write", "y.read"],
            }
        }
    )
    issuer_claims = {"email": "user@example.com", "email_verified": True}

    if base_build:

        async def collect() -> Any:
            return await core.collect_claims(
                USER,
                session,
                issuer="identity",
                precomputed=pre,
                issuer_claims=issuer_claims,
            )

        def create(extra: Any) -> str:
            return tokens.create_access_token({**extra, **ENVELOPE})

        def make_plan() -> Any:
            return core._claims_plan(USER, "identity", session, pre)

        def merge(plan: Any, outcomes: Any) -> Any:
            return core._merge_claim_outcomes(
                plan, outcomes, issuer="identity", issuer_claims=issuer_claims
            )

    else:

        async def collect() -> Any:
            return await core.collect_claims(USER, session, issuer=identity_fn)

        def create(extra: Any) -> str:
            return tokens.create_access_token(ENVELOPE, claims=extra)

        def make_plan() -> Any:
            return core._claims_plan(USER, "identity", session)

        def merge(plan: Any, outcomes: Any) -> Any:
            return core._merge_claim_outcomes(plan, outcomes)

    plan = make_plan()
    outcomes = await fc._run_claims_plan(plan)
    merged = merge(plan, outcomes)
    sealed = await collect()

    try:
        from sandloom.core.claims import _seal_claims
    except ImportError:
        _seal_claims = None  # type: ignore[assignment]
    issuer_index = next(
        i for i, step in enumerate(plan) if getattr(step[2], fc._ISSUER_ATTR, False)
    )
    local_index = next(i for i in range(len(plan)) if i != issuer_index)
    issuer_call = plan[issuer_index][2]
    local_call = plan[local_index][2]

    async def op() -> Any:
        if part == "issuer":
            return await issuer_call()
        if part == "local":
            return await local_call()
        if part == "attempt":
            return await fc._attempt_claims("x", local_call)
        if part == "resolve":
            return core._resolve_issuer(identity_fn)
        if part == "require":
            return core._require_map_issuer("identity")
        if part == "plan":
            return make_plan()
        if part == "run":
            return await fc._run_claims_plan(plan)
        if part == "merge":
            return merge(plan, outcomes)
        if part == "seal":
            return _seal_claims(dict(merged))
        if part == "dictcopy":
            return dict(merged)
        if part == "create":
            return create(sealed)
        if part == "collect":
            return await collect()
        if part == "mint":
            return create(await collect())
        if part == "none":
            return None
        raise SystemExit(f"unknown part {part}")

    for _ in range(WARMUP):
        await op()
    gc.collect()
    gc.freeze()
    for _ in range(count):
        await op()


def _instructions(part: str, count: int) -> int:
    """Instructions of one cachegrind run of this file's ``--part`` mode."""
    run = subprocess.run(
        [
            "valgrind",
            "--tool=cachegrind",
            "--cache-sim=no",
            "--cachegrind-out-file=/dev/null",
            sys.executable,
            str(Path(__file__).resolve()),
            "--part",
            part,
            str(count),
        ],
        capture_output=True,
        text=True,
        cwd="/tmp",
        env=dict(os.environ, PYTHONHASHSEED="0"),
        check=False,
    )
    found = re.search(r"I\s+refs:\s+([\d,]+)", run.stderr)
    if not found or run.returncode != 0:
        raise RuntimeError(run.stderr[-3000:])
    return int(found.group(1).replace(",", ""))


def _measure(parts: list[str]) -> None:
    """In the container: print Ir per operation of each part."""
    k = int(os.environ.get("CG_K", "1000"))
    for part in parts:
        base, full = _instructions(part, 0), _instructions(part, k)
        print(f"{part:8s} {(full - base) / k:12,.0f} instr/op", flush=True)


def _host(argv: list[str]) -> None:
    """Run the measurement in the cachegrind image, on a checkout's ``src``."""
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="three-owner sealed mint, Ir per op")
    parser.add_argument("--tree", type=Path, default=root, help="checkout root")
    parser.add_argument("--k", type=int, default=1000, help="measured operations")
    parser.add_argument(
        "--nopre",
        action="store_true",
        help="a base build runs the issuer's contributor",
    )
    parser.add_argument("parts", nargs="*", default=DEFAULT_PARTS)
    args = parser.parse_args(argv)
    tree = args.tree.resolve()
    claims = (tree / "src/sandloom/core/facade/_claims.py").read_text()
    subprocess.run(
        ["docker", "build", "-q", "-t", IMAGE, str(Path(__file__).parent / "cg")],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    command = [
        "docker",
        "run",
        "--rm",
        "-e",
        "DATABASE_URL=postgresql+asyncpg://x:x@host.docker.internal:5432/x",
        "-e",
        "JWT_SECRET=mintprobe-secret-not-a-real-one-0123456789abcdef",
        "-e",
        "ENVIRONMENT=dev",
        "-e",
        f"CG_K={args.k}",
        "-e",
        "PYTHONPATH=/w/src",
        "-v",
        f"{tree}/src:/w/src:ro",
        "-v",
        f"{Path(__file__).resolve().parent}:/p:ro",
        "-w",
        "/tmp",
    ]
    if args.nopre:
        command += ["-e", "PROBE_NOPRE=1"]
    if "precomputed:" in claims:
        # a build from before the INFRA move still reads the flat urls
        command += [
            "-e",
            "CACHE_URL=redis://host.docker.internal:6379/0",
            "-e",
            "BROKER_URL=redis://host.docker.internal:6379/1",
        ]
    command += [IMAGE, "python", "/p/kmint_probe.py", "--measure", *args.parts]
    subprocess.run(command, check=True)


def main(argv: list[str]) -> None:
    """Dispatch: ``--part`` and ``--measure`` run in the container."""
    if argv[:1] == ["--part"]:
        asyncio.run(_run_part(argv[1], int(argv[2])))
    elif argv[:1] == ["--measure"]:
        _measure(argv[1:])
    else:
        _host(argv)


if __name__ == "__main__":
    main(sys.argv[1:])
