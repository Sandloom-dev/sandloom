"""T-14: the trusted view adds no I/O to a request (ADR-0014 decision 3).

``Role``, ``Perm`` and ``TenantScope`` on a verifier read only the token's
trusted view: per request **DB 0, cache 1 round trip (the revocation
``GET``), RPC 0**, in the monolith (both authorities loaded) and in a split
service (neither loaded, the grant's authority bound remote), on the first
request of a token (view computed) and on a repeat (view cached).

Counted where each kind of I/O leaves the process: every SQL statement of
any engine (SQLAlchemy ``before_cursor_execute`` on ``Engine``) and every
database handle the verifier asks for, every cache
call (the counting cache of the revocation tests: ``get`` / ``get_many``,
one round trip each) and every outbound HTTP request of a real transport
(the internal RPC client; the in-process test client is not one).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from sandloom.core.config.settings import settings
from sandloom.core.runtime import use_active_core
from sandloom.core.tests import test_claims_split_matrix as _matrix
from sandloom.core.tests.test_claims_split_matrix import (
    BINDINGS,
    GRANTS,
    ID,
    Issuer,
    roles_map,
    verifier,
)

caller = _matrix.caller
mem_cache = _matrix.mem_cache
issuers = _matrix.issuers
_fresh_verification_cache = _matrix._fresh_verification_cache
_close_shared_client = _matrix._close_shared_client
_standard_entry_points = _matrix._standard_entry_points

GATES = ("/role", "/perm", "/tenant")


class Counts:
    def __init__(self, cache: Any) -> None:
        self.cache = cache
        self.sql = 0
        self.rpc = 0
        self._cache_base = 0

    def reset(self) -> None:
        self.sql = self.rpc = 0
        self._cache_base = self.cache.gets

    def snapshot(self) -> dict[str, int]:
        return {
            "db": self.sql,
            "cache": self.cache.gets - self._cache_base,
            "rpc": self.rpc,
        }


@pytest.fixture
def counts(mem_cache: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Counts]:
    found = Counts(mem_cache)

    def on_sql(*_args: Any, **_kwargs: Any) -> None:
        found.sql += 1

    for transport in (httpx.AsyncHTTPTransport, httpx.HTTPTransport):
        attr = (
            "handle_async_request"
            if transport is httpx.AsyncHTTPTransport
            else "handle_request"
        )
        real = getattr(transport, attr)

        def counted(self: Any, request: Any, _real: Any = real) -> Any:
            found.rpc += 1
            return _real(self, request)

        monkeypatch.setattr(transport, attr, counted)
    event.listen(Engine, "before_cursor_execute", on_sql)
    try:
        yield found
    finally:
        event.remove(Engine, "before_cursor_execute", on_sql)


PLACEMENTS: dict[str, tuple[dict[str, str], dict[str, Any]]] = {
    "mono": ({"identity": ID, "grants": GRANTS}, {}),
    "split": ({}, BINDINGS),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("placement", sorted(PLACEMENTS))
async def test_role_perm_and_tenant_scope_cost_one_cache_get(
    issuers: Callable[..., Issuer],
    counts: Counts,
    monkeypatch: pytest.MonkeyPatch,
    placement: str,
) -> None:
    loaded, bindings = PLACEMENTS[placement]
    token = await issuers(ID).mint(uuid4())
    monkeypatch.setattr(settings, "service_bindings", bindings)
    core = verifier(roles_map(ID), loaded=loaded)

    def database(name: str = "default") -> Any:
        counts.sql += 1  # a database handle asked for counts as DB I/O
        raise AssertionError(f"the verifier asked for database {name!r}")

    core.database = database  # type: ignore[method-assign]
    for attempt in ("view computed", "view cached"):
        for gate in GATES:
            counts.reset()
            status = await _single(core, token, gate)
            assert status == 200, (gate, attempt)
            assert counts.snapshot() == {"db": 0, "cache": 1, "rpc": 0}, (
                gate,
                attempt,
            )


async def _single(core: Any, token: str, gate: str) -> int:
    with use_active_core(core):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_matrix.GATED),
            base_url="http://verifier",
        ) as client:
            response = await client.get(
                gate, headers={"Authorization": f"Bearer {token}"}
            )
    return response.status_code


@pytest.mark.asyncio
async def test_the_counter_sees_a_real_transport(counts: Counts) -> None:
    """Control: an outbound request on a real transport is counted (the
    request fails fast on a closed port; the attempt is what counts)."""
    async with httpx.AsyncClient(timeout=0.5) as client:
        with pytest.raises(httpx.HTTPError):
            await client.get("http://127.0.0.1:9/")
    assert counts.rpc == 1
