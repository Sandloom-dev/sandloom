"""Claims-tier cachegrind rungs (``K*``): the trusted claims view (KB-11).

Installed into ``ladder.py`` like ``infra_rungs.py``. One kernel process
(``CoreFacade``) holds a claims map (``roles`` / ``permissions`` from module
``bench.identity``, ``tenant_ids`` from ``bench.grants``, both local), mints
real signed access tokens through ``collect_claims`` + ``create_access_token``
and verifies them through the kernel gates. No database and no network: the
revocation ``GET`` goes to an in-memory cache, so the numbers are the
authorization's own cost. The same file runs on a build before the view (the
tokens then carry no ``cpv`` and every claim is trusted as it stood) and on
one with it.

=========  ==================================================================
rung       one op
=========  ==================================================================
``KRole``  ASGI request through ``require(Role("admin"))``, one token repeated
           (the verified-token cache answers: the view is cached)
``KPerm``  the same through ``require(Perm("x.read"))``
``KTen``   the same through ``TenantScope()``
``KRoleC`` ``KRole`` with a fresh token every op (signature check + view)
``KTenC``  ``KTen`` with a fresh token every op
``KDec``   ``decode_token(token)`` on a cached token (the view's per-request
           check, no ASGI)
``KDecC``  ``decode_token`` of a fresh token (verify + view + cache fill)
``KVerify`` the signature and claims check of a fresh token alone
``KView``  ``apply_view`` of verified claims alone (builds with the view)
``KLoads`` ``orjson.loads`` of the verified claims (what a cache hit decodes)
``KCtx``   ``UserContext(**claims)`` of the verified claims
``KMintC`` ``collect_claims(user)``: the merge, with ``cpv`` where stamped
``KMint``  ``collect_claims`` + ``create_access_token``: one mint
=========  ==================================================================

Fresh tokens are minted in the setup, a fixed count (``CG_K`` + 40), so the
setup cancels in Ir(K) - Ir(0).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from typing import Any

_POOL = int(os.environ.get("CG_K", "200")) + 40
IDENTITY = "bench.identity"
GRANTS = "bench.grants"
USER = uuid.UUID(int=0xC1A1)
TENANT = uuid.UUID(int=0x7E4A)
HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"host", b"127.0.0.1:8000"),
    (b"user-agent", b"ladder"),
    (b"accept", b"*/*"),
)


class _Cache:
    """Revocation reads only: ``get`` / ``get_many``, no marks set."""

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
    def uow_factory(self) -> Callable[[], _UoW]:
        return _UoW

    def outbox(self) -> None:
        return None

    def audit(self) -> None:
        return None


def _gated_app() -> Any:
    from fastapi import Depends, FastAPI

    from sandloom.core.di import TenantScope
    from sandloom.shared.authz import Perm, Role, require

    app = FastAPI()

    @app.get("/role", dependencies=[Depends(require(Role("admin")))])
    async def _role() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/perm", dependencies=[Depends(require(Perm("x.read")))])
    async def _perm() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/tenant")
    async def _tenant(tenant: Any = TenantScope()) -> dict[str, str]:
        return {"tenant": str(tenant)}

    return app


def _core() -> Any:
    from fastapi import FastAPI

    import sandloom.core.infra.access as access
    import sandloom.core.schema.registry as registry
    from sandloom.core.app_factory.claim_authorities import (
        build_claim_authorities,
        filter_to_composition,
    )
    from sandloom.core.eventing.event_bus import set_active_event_bus
    from sandloom.core.eventing.memory_bus import InMemoryEventBus
    from sandloom.core.facade import CoreFacade
    from sandloom.core.runtime import set_active_core

    # prod has no bus fallback; the contract runtime only needs one bound
    set_active_event_bus(InMemoryEventBus(registry.InMemorySchemaRegistry()))
    core = CoreFacade(app=FastAPI())
    core.database = lambda name="default": _Database()  # type: ignore[method-assign]
    mapping = {
        "issuer": IDENTITY,
        "roles": IDENTITY,
        "permissions": IDENTITY,
        "tenant_ids": GRANTS,
    }
    core._assign_claim_authorities(
        filter_to_composition(
            build_claim_authorities(mapping),
            loaded={"identity": IDENTITY, "grants": GRANTS},
            bindings={},
        )
    )

    async def roles(_user_id: Any) -> dict[str, Any]:
        return {"roles": ["admin"], "permissions": ["x.read"]}

    async def grants(_user_id: Any) -> dict[str, Any]:
        return {"tenant_ids": [str(TENANT)]}

    with core.module_registration(IDENTITY) as api:
        api.add_claims_contributor(roles, critical=True)
    with core.module_registration(GRANTS) as api:
        api.add_claims_contributor(grants, critical=True)
    fake = _Cache()
    access.cache = lambda name="default": fake  # type: ignore[assignment]
    set_active_core(core)
    return core


async def _mint(core: Any) -> str:
    from sandloom.core.auth import tokens

    claims = await core.collect_claims(USER)
    return tokens.create_access_token(
        {"sub": str(USER), "token_type": "access", **claims}
    )


def _asgi(app: Any, path: str, tokens: list[str]) -> Callable[[int], Any]:
    base = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "query_string": b"",
        "root_path": "",
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
        "path": path,
        "raw_path": path.encode(),
    }
    heads = [[*HEADERS, (b"authorization", f"Bearer {t}".encode())] for t in tokens]
    req = {"type": "http.request", "body": b"", "more_body": False}

    async def receive() -> Any:
        return req

    async def op(i: int) -> Any:
        out: list[Any] = []

        async def send(msg: Any) -> None:
            out.append(msg)

        scope = dict(base)
        scope["headers"] = heads[i % len(heads)]
        scope["state"] = {}
        await app(scope, receive, send)
        if out[0]["status"] != 200:
            raise RuntimeError(f"{path}: {out}")
        return out[1]["body"]

    return op


async def _noop() -> None:
    return None


async def _request(_ns: Any, path: str, fresh: bool) -> Any:
    core = _core()
    app = _gated_app()
    pool = [await _mint(core) for _ in range(_POOL if fresh else 1)]
    return _asgi(app, path, pool), _noop


async def _decode(_ns: Any, fresh: bool) -> Any:
    from sandloom.core.auth import tokens

    core = _core()
    pool = [await _mint(core) for _ in range(_POOL if fresh else 1)]
    if not fresh:
        assert tokens.decode_token(pool[0]) is not None

    async def op(i: int) -> Any:
        claims = tokens.decode_token(pool[i % len(pool)])
        if claims is None:
            raise RuntimeError("token did not verify")
        return claims

    return op, _noop


async def _cold_parts(_ns: Any, part: str) -> Any:
    """The pieces of a fresh decode: the signature and claims check alone
    (``verify``: ``_decode_token_uncached``, a build with or without the
    view) and the view alone (``view``: ``apply_view`` on a copy of verified
    claims, the build with the view only)."""
    from sandloom.core.auth import tokens

    core = _core()
    pool = [await _mint(core) for _ in range(_POOL)]
    if part == "verify":

        async def verify(i: int) -> Any:
            return tokens._decode_token_uncached(pool[i % len(pool)])

        return verify, _noop

    import time

    from sandloom.core.auth import claims_view
    from sandloom.core.config.settings import settings

    policy = core.claim_authorities
    verified = [tokens._decode_token_uncached(t) for t in pool]

    async def view(i: int) -> Any:
        return claims_view.apply_view(
            dict(verified[i % len(verified)]),
            policy,
            now=time.time(),
            lifetime_seconds=settings.access_token_expire_minutes * 60,
            leeway_seconds=settings.jwt_leeway_seconds,
        )

    return view, _noop


async def _parts(_ns: Any, part: str) -> Any:
    """The pieces of a cached request: the cache's ``orjson.loads`` of the
    verified claims (``loads``) and ``UserContext(**claims)`` (``ctx``)."""
    import orjson

    from sandloom.core.auth import tokens
    from sandloom.shared.subject import UserContext

    core = _core()
    token = await _mint(core)
    claims = tokens.decode_token(token)
    assert claims is not None
    raw = orjson.dumps(claims)

    async def loads(i: int) -> Any:
        return orjson.loads(raw)

    async def ctx(i: int) -> Any:
        return UserContext(**claims)

    return (loads if part == "loads" else ctx), _noop


async def _mint_rung(_ns: Any, with_token: bool) -> Any:
    core = _core()

    async def op(i: int) -> Any:
        if with_token:
            return await _mint(core)
        return await core.collect_claims(USER)

    await op(0)
    return op, _noop


def install(ns: Any) -> None:
    """Register the stand's rungs in ``ladder.RUNGS`` (``ns`` = ladder module)."""

    def add(name: str, desc: str, build: Any, *args: Any) -> None:
        async def factory() -> Any:
            return await build(ns, *args)

        ns.RUNGS[name] = (desc, factory)

    add("KRole", "ASGI request, require(Role), token cached", _request, "/role", False)
    add("KPerm", "ASGI request, require(Perm), token cached", _request, "/perm", False)
    add("KTen", "ASGI request, TenantScope, token cached", _request, "/tenant", False)
    add("KRoleC", "ASGI request, require(Role), fresh token", _request, "/role", True)
    add("KTenC", "ASGI request, TenantScope, fresh token", _request, "/tenant", True)
    add("KDec", "decode_token on a cached token", _decode, False)
    add("KDecC", "decode_token of a fresh token", _decode, True)
    add("KVerify", "signature + claims check only (fresh token)", _cold_parts, "verify")
    add(
        "KView",
        "apply_view on verified claims (builds with the view)",
        _cold_parts,
        "view",
    )
    add(
        "KLoads", "orjson.loads of the verified claims (the cache hit)", _parts, "loads"
    )
    add("KCtx", "UserContext(**claims) of the verified claims", _parts, "ctx")
    add("KMintC", "collect_claims: the claims merge", _mint_rung, False)
    add("KMint", "collect_claims + create_access_token", _mint_rung, True)
