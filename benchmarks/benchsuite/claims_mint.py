"""Access tokens for the stands, minted through the real path (ADR-0014).

A verifier trusts a protected claim (``tenant_ids``, ``subscription_status``,
``roles``, ...) only when the token carries the ``cpv`` digest and the
provenance of the claim's assigned authority, which only ``collect_claims`` +
``create_access_token(claims=sealed)`` writes. A hand-assembled HS256 JWT has
neither, so the stand answers it 401 ``token_policy_outdated`` (402 on a
``Subscribed`` route). Every stand mints here instead; the sealed mint is
not touched.

``bench``     the S5-family stands (``SANDLOOM_MODULES=["benchsuite.bench"]``):
              a kernel core with the stand's map
              (:data:`benchsuite.bench.claims.CLAIM_AUTHORITIES`) and the
              ``bench`` module's contributor. No database, no network.
``platform``  the platform stands (identity + tenancy + billing, and the
              example apps): the standard app of the caller's environment is
              built and ``collect_claims`` reads the user's real roles,
              tenancy grant and subscription from the seeded database.

Each mint runs in its own process: it binds a core and an event bus
process-wide, which must not touch an app the calling process may be running.

    python -m benchsuite.claims_mint bench --tenant <uuid> [--subscription active|none]
    python -m benchsuite.claims_mint platform --user <uuid> --hours 24
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

_PACKAGE_ROOT = str(Path(__file__).resolve().parents[1])


# --------------------------------------------------------------------------- #
# The calling side: one subprocess per mint
# --------------------------------------------------------------------------- #
def _run(args: list[str], env: Mapping[str, str] | None = None) -> str:
    merged = {**os.environ, **(env or {})}
    merged["PYTHONPATH"] = os.pathsep.join(
        p for p in (_PACKAGE_ROOT, merged.get("PYTHONPATH", "")) if p
    )
    merged.setdefault("SANDLOOM_SERVICE_NAME", "ladder")
    done = subprocess.run(  # noqa: S603 - this interpreter, fixed module, no shell
        [sys.executable, "-m", "benchsuite.claims_mint", *args],
        env=merged,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode:
        raise RuntimeError(
            f"claims mint failed ({done.returncode}):\n{done.stderr[-2000:]}"
        )
    lines = done.stdout.strip().splitlines()
    return lines[-1]  # logs may precede the token


def mint_bench(
    tenant: str, *, subscription: str | None = "active", hours: int = 6
) -> str:
    """A tenant ``tenant`` access token of the ``bench`` stands."""
    return _run(
        [
            "bench",
            "--tenant",
            tenant,
            "--subscription",
            "none" if subscription is None else subscription,
            "--hours",
            str(hours),
        ]
    )


def mint_platform(
    user: uuid.UUID, *, hours: int = 6, env: Mapping[str, str] | None = None
) -> str:
    """An access token for ``user`` of the platform stand ``env`` describes
    (its modules and database), claims read from the seeded database."""
    return _run(["platform", "--user", str(user), "--hours", str(hours)], env)


# --------------------------------------------------------------------------- #
# The minting side
# --------------------------------------------------------------------------- #
class _Cache:
    async def get(self, key: str) -> None:
        return None

    async def get_many(self, keys: list[str]) -> list[None]:
        return [None for _ in keys]


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
    """The contributor reads nothing: a unit that never reaches a database."""

    def uow_factory(self) -> Callable[[], _UoW]:
        return _UoW

    def outbox(self) -> None:
        return None

    def audit(self) -> None:
        return None


def _bench_core(tenant: str, subscription: str | None) -> Any:
    from fastapi import FastAPI

    import sandloom.core.infra.access as access
    import sandloom.core.schema.registry as registry
    from benchsuite.bench import claims
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
    core._assign_claim_authorities(
        filter_to_composition(
            build_claim_authorities(claims.CLAIM_AUTHORITIES),
            loaded={"bench": claims.AUTHORITY},
            bindings={},
        )
    )
    with core.module_registration(claims.AUTHORITY) as api:
        api.add_claims_contributor(
            claims.contributor(tenant, subscription), critical=True
        )
    access.cache = lambda name="default": _Cache()  # type: ignore[assignment]
    set_active_core(core)
    return core


async def _mint(core: Any, user: uuid.UUID, hours: int) -> str:
    from sandloom.core.auth import tokens

    sealed = await core.collect_claims(user)
    return tokens.create_access_token(
        {"sub": str(user), "token_type": "access"}, hours * 60, claims=sealed
    )


def _bench(args: argparse.Namespace) -> str:
    subscription = None if args.subscription == "none" else args.subscription
    core = _bench_core(args.tenant, subscription)
    return asyncio.run(_mint(core, uuid.uuid4(), args.hours))


def _platform(args: argparse.Namespace) -> str:
    # The standard root of the caller's environment: its own modules, trust
    # map and database; the lifespan starts what ``collect_claims`` runs on.
    from sandloom_standard.main import app

    async def mint() -> str:
        async with app.router.lifespan_context(app):
            return await _mint(app.state.core, uuid.UUID(args.user), args.hours)

    return asyncio.run(mint())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="stand", required=True)
    bench = sub.add_parser("bench")
    bench.add_argument("--tenant", required=True)
    bench.add_argument("--subscription", default="active")
    bench.add_argument("--hours", type=int, default=6)
    platform = sub.add_parser("platform")
    platform.add_argument("--user", required=True)
    platform.add_argument("--hours", type=int, default=6)
    args = parser.parse_args(argv)
    token = _bench(args) if args.stand == "bench" else _platform(args)
    print(token)


if __name__ == "__main__":
    main()
