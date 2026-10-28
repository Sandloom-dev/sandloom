"""The shared pooled outbound HTTP client (``RPC_TRANSPORT``) and the lazily
imported transport modules.
"""

from __future__ import annotations

import importlib
from typing import Any

from sandloom.core import rpc_transport_compat
from sandloom.core.config.settings import settings as _global_settings

#: ``httpx`` and :mod:`sandloom.core.rpc_transport` (which imports it) load on the
#: first outbound call, not with this module: ``httpx/__init__`` imports its
#: CLI (click, pygments, rich), ~95 ms of every ``import sandloom.core.di``.
_LAZY_MODULES: dict[str, Any] = {}


def _lazy_module(name: str) -> Any:
    module = _LAZY_MODULES.get(name)
    if module is None:
        module = _LAZY_MODULES[name] = importlib.import_module(name)
    return module


def _httpx() -> Any:
    return _lazy_module("httpx")


def _transport() -> Any:
    return _lazy_module("sandloom.core.rpc_transport")


def close_later(close: Any) -> None:
    """:func:`sandloom.core.rpc_transport.close_later` (resolved lazily)."""
    _transport().close_later(close)


def _discard_pool(client: Any) -> None:
    """No NEW call may use ``client``'s connections (in-flight ones finish)."""
    global _rpc_client_instance, _rpc_client_kind
    reset = getattr(client, "reset_pool", None)
    if callable(reset):
        reset()
        return
    if client is _rpc_client_instance:
        _rpc_client_instance, _rpc_client_kind = None, None
        _retire_client(client)


# One pooled, keep-alive client shared by every outbound RPC call (a per-call
# client paid a fresh TCP+TLS handshake on each invocation). ``RPC_TRANSPORT``
# picks it: ``aiohttp`` (default) = :mod:`sandloom.core.rpc_transport`,
# ``httpx`` = the previous ``httpx.AsyncClient``. Both expose the same
# ``post(url, content=, headers=, timeout=)`` / ``get(url, headers=, timeout=)``
# surface and raise httpx's exception classes, so the proxy below is
# transport-agnostic; per-call timeouts are passed per request.
_rpc_client_instance: Any = None


_rpc_client_kind: str | None = None


def _rpc_client() -> Any:
    global _rpc_client_instance, _rpc_client_kind
    client = _rpc_client_instance
    kind = getattr(_global_settings, "rpc_transport", "aiohttp")
    if client is None or client.is_closed or kind != _rpc_client_kind:
        if client is not None and not client.is_closed:
            _retire_client(client)
        client = _new_rpc_client(_effective_transport(kind))
        _rpc_client_instance, _rpc_client_kind = client, kind
    return client


def _effective_transport(kind: str) -> str:
    """``kind``, or ``httpx`` when the aiohttp internals probe failed."""
    if kind == "aiohttp" and not rpc_transport_compat.aiohttp_usable():
        return "httpx"
    return kind


async def check_rpc_transport() -> None:
    """Startup: run the full aiohttp probe (incl. the loopback self-test)
    when this process makes outbound RPC calls; on failure the shared client
    is rebuilt on httpx (never fails startup)."""
    global _rpc_client_kind
    cfg = _global_settings
    if getattr(cfg, "rpc_transport", "aiohttp") != "aiohttp":
        return
    if not getattr(cfg, "service_bindings", None):
        return
    result = await rpc_transport_compat.full_probe()
    if not result.ok:
        _rpc_client_kind = None  # the next call rebuilds the client (httpx)


_retired_clients: list[Any] = []


def _retire_client(client: Any) -> None:
    """Let in-flight calls finish, then close AND release the retired client."""
    _retired_clients[:] = [each for each in _retired_clients if not each.is_closed]
    _retired_clients.append(client)

    async def finish() -> None:
        try:
            if not client.is_closed:
                await client.aclose()
        finally:
            _retired_clients[:] = [
                each for each in _retired_clients if each is not client
            ]

    close_later(finish)


def _new_rpc_client(kind: str) -> Any:
    """A fresh pooled client of ``RPC_TRANSPORT`` ``kind``."""
    cfg = _global_settings
    if kind == "httpx":
        from sandloom.core.rpc_transport import HttpxRpcClient  # noqa: PLC0415 - cold: rpc_transport imports httpx; it loads on the first outbound call (test_rpc_lazy_httpx)

        return HttpxRpcClient(
            max_connections=int(getattr(cfg, "rpc_pool_max_connections", 100)),
            max_response_bytes=int(
                getattr(cfg, "rpc_max_response_bytes", 32 * 1024 * 1024)
            ),
            # An automatic fallback must not silently route credentials through
            # an ambient proxy. Explicit httpx keeps its documented proxy mode.
            trust_env=getattr(cfg, "rpc_transport", "aiohttp") == "httpx",
        )
    from sandloom.core.rpc_transport import CLIENTS  # noqa: PLC0415 - cold: rpc_transport imports httpx; it loads on the first outbound call (test_rpc_lazy_httpx)

    if kind not in CLIENTS:
        raise ValueError(f"unknown RPC_TRANSPORT {kind!r}")
    return CLIENTS[kind](
        max_connections=int(getattr(cfg, "rpc_pool_max_connections", 100)),
        idle_seconds=float(getattr(cfg, "rpc_pool_idle_seconds", 4.0)),
        max_response_bytes=int(
            getattr(cfg, "rpc_max_response_bytes", 32 * 1024 * 1024)
        ),
    )


async def close_rpc_client() -> None:
    """Dispose the shared RPC client's connection pool (a shutdown task)."""
    global _rpc_client_instance, _rpc_client_kind
    client, _rpc_client_instance, _rpc_client_kind = _rpc_client_instance, None, None
    retired = [*_retired_clients, client]
    _retired_clients.clear()
    for each in retired:
        if each is not None and not each.is_closed:
            await each.aclose()
