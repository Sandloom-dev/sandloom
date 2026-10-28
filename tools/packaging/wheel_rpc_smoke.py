"""Internal-RPC transport smoke test against an INSTALLED sandloom wheel.

Run with the interpreter of a clean venv that has only the wheel installed,
in isolated mode from a directory outside the source tree::

    <venv>/bin/python -I tools/packaging/wheel_rpc_smoke.py

(``tools/packaging/check_wheel_rpc_transport.py`` builds the wheel and does exactly
that; ``.github/workflows/packages.yml`` runs it on the lean install.) It
proves, from the distribution rather than the monorepo source:

1. ``sandloom`` is imported from site-packages and the aiohttp dependency is
   installed inside ``VERIFIED_AIOHTTP_RANGE``;
2. the full aiohttp probe (static + instance + loopback self-test) passes and
   a real RPC call to a live provider (uvicorn, pair-MAC authentication and
   keyed response binding) goes through the aiohttp client;
3. with aiohttp's internals changed (a private method removed — the shape of
   an incompatible upgrade) the probe fails, the shared client is rebuilt on
   httpx and the same call still succeeds.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

# Key generation needs no settings (the env below is read on first use).
from sandloom.core.signing_keys import (
    generate_private_key,
    generate_x25519_key,
    private_pem,
    public_pem,
    x25519_private_pem,
    x25519_public_pem,
)

SERVICE = "smoke.echo.v1"
#: The smoke process calls ITSELF: one RPC service identity on both sides.
SERVICE_ID = "smoke"
_SIGNING = generate_private_key("EdDSA")
_DH = generate_x25519_key()

os.environ.update(
    {
        "ENVIRONMENT": "dev",
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
        "CACHE_URL": "redis://localhost:6379/0",
        "BROKER_URL": "redis://localhost:6379/1",
        "JWT_SECRET": "wheel-rpc-smoke-not-a-real-secret",
        "EVENT_BUS_BACKEND": "memory",
        "SANDLOOM_MODULES": "[]",
        "RPC_TRANSPORT": "aiohttp",
        "RPC_SERVICE_ID": SERVICE_ID,
        "RPC_PRIVATE_KEY": private_pem(_SIGNING),
        "RPC_KEY_ID": "k1",
        "RPC_DH_PRIVATE_KEY": x25519_private_pem(_DH),
        "RPC_DH_KEY_ID": "k1",
        "RPC_SERVICE_KEYS": json.dumps({SERVICE_ID: {"k1": public_pem(_SIGNING)}}),
        "RPC_SERVICE_DH_KEYS": json.dumps({SERVICE_ID: {"k1": x25519_public_pem(_DH)}}),
        "RPC_ALLOWED_CALLS": json.dumps({SERVICE_ID: [SERVICE]}),
    }
)


def _check(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAIL: {message}", file=sys.stderr)
        raise SystemExit(1)
    print(f"ok: {message}")


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split(".")[:3] if p.isdigit())


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
    """No database: the smoke contract never touches its session."""

    def uow_factory(self) -> Any:
        return _UoW

    def outbox(self) -> None:
        return None

    def audit(self) -> None:
        return None


def _provider_app() -> Any:
    from fastapi import FastAPI

    from sandloom.core.facade import CoreFacade
    from sandloom.core.module_api import contract_method
    from sandloom.core.rpc import install_service_rpc_routes

    app = FastAPI()
    core = CoreFacade(app=app)
    app.state.core = core
    core.database = lambda name="default": _Database()  # type: ignore[method-assign]

    class Echo:
        @classmethod
        @contract_method
        async def echo(cls, value: str) -> str:
            return f"echo:{value}"

    core.register_service(SERVICE, Echo)
    install_service_rpc_routes(app, core)
    return app


async def _main() -> None:
    import httpx
    import uvicorn

    import sandloom
    from sandloom.core import rpc, rpc_transport_compat
    from sandloom.core.rpc import client as rpc_client
    from sandloom.core.rpc_transport import AiohttpRpcClient

    # 1. the installed distribution, not the source tree
    origin = Path(sandloom.__file__).resolve()
    _check(
        "site-packages" in origin.parts, f"sandloom imported from the wheel ({origin})"
    )
    source_tree = Path(__file__).resolve().parents[2] / "src"
    _check(source_tree not in origin.parents, "not from the monorepo source tree")
    aiohttp_version = metadata.version("aiohttp")
    low, high = rpc_transport_compat.VERIFIED_AIOHTTP_RANGE
    _check(
        _version_tuple(low) <= _version_tuple(aiohttp_version) < _version_tuple(high),
        f"aiohttp {aiohttp_version} installed as a dependency, in [{low}, {high})",
    )
    requires = metadata.requires("sandloom") or []
    _check(
        any(re.match(r"aiohttp\s*[<>=!~]", r) for r in requires),
        "sandloom declares the aiohttp dependency",
    )

    # a live provider over real HTTP
    config = uvicorn.Config(
        _provider_app(), host="127.0.0.1", port=0, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    proxy = rpc.RemoteServiceProxy(
        service_name=SERVICE,
        binding={"base_url": f"http://127.0.0.1:{port}", "service_id": SERVICE_ID},
        default_timeout_seconds=5.0,
    )
    try:
        # 2. the aiohttp transport, verified
        probe = await rpc_transport_compat.full_probe()
        _check(probe.ok, f"full aiohttp probe passes ({probe.problems or 'clean'})")
        _check(
            isinstance(rpc_client._rpc_client(), AiohttpRpcClient),
            "the shared RPC client is the aiohttp transport",
        )
        _check(await proxy.echo("a") == "echo:a", "RPC call over aiohttp")

        # 3. an internal-API mismatch -> httpx fallback, calls keep working
        from aiohttp.client_proto import ResponseHandler

        await rpc.close_rpc_client()
        rpc_transport_compat.reset_transport_probes()
        removed = ResponseHandler.force_close
        del ResponseHandler.force_close
        try:
            probe = rpc_transport_compat.static_probe()
            _check(
                not probe.ok and probe.reason == rpc_transport_compat.REASON_INTERNALS,
                "a changed aiohttp internal is detected (internals_changed)",
            )
            _check(
                isinstance(rpc_client._rpc_client(), httpx.AsyncClient),
                "the shared RPC client falls back to httpx",
            )
            _check(await proxy.echo("b") == "echo:b", "RPC call over the fallback")
        finally:
            ResponseHandler.force_close = removed  # type: ignore[method-assign]
    finally:
        await rpc.close_rpc_client()
        server.should_exit = True
        await serving
    print("wheel RPC transport smoke: OK")


if __name__ == "__main__":
    asyncio.run(_main())
