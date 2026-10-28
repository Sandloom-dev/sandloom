"""The ``sandloom.core.rpc`` package: its public re-exports and its patch guard.

The package re-exports only its public API; private helpers are reached
through their submodule (``from sandloom.core.rpc import wire``).
"""

from __future__ import annotations

import pytest

from sandloom.core import rpc
from sandloom.core.rpc import client, errors, proxy, readiness, server


def test_reexports_are_the_submodules_objects() -> None:
    assert rpc.RemoteServiceProxy is proxy.RemoteServiceProxy
    assert rpc.remote_service_proxy is proxy.remote_service_proxy
    assert rpc.RemoteServiceUnavailable is errors.RemoteServiceUnavailable
    assert rpc.RemoteServiceBadResponse is errors.RemoteServiceBadResponse
    assert rpc.check_rpc_transport is client.check_rpc_transport
    assert rpc.close_rpc_client is client.close_rpc_client
    assert rpc.check_remote_service_bindings is (
        readiness.check_remote_service_bindings
    )
    assert rpc.RpcFastPathMiddleware is server.RpcFastPathMiddleware
    assert rpc.rpc_router is server.rpc_router
    for name in rpc.__all__:
        assert getattr(rpc, name) is not None


def test_private_names_are_not_reexported() -> None:
    for name in ("_unpack", "_pack", "_rpc_client", "_remote_contracts"):
        assert not hasattr(rpc, name), name
    assert not hasattr(rpc, "rpc_idempotency_key")


def test_patching_a_looked_up_name_on_the_package_is_refused() -> None:
    # A public re-export: rebinding the package's copy would patch nothing.
    original = rpc.RemoteServiceProxy
    with pytest.raises(
        AttributeError, match=r"sandloom\.core\.rpc\.proxy\.RemoteServiceProxy"
    ):
        rpc.RemoteServiceProxy = object()  # type: ignore[assignment,misc]
    assert rpc.RemoteServiceProxy is original
    with pytest.raises(
        AttributeError, match=r"sandloom\.core\.rpc\.client\.close_rpc_client"
    ):
        rpc.close_rpc_client = object()  # type: ignore[assignment]
    # A submodule global the package does not even export is refused too.
    with pytest.raises(AttributeError, match=r"sandloom\.core\.rpc\.server\._unpack"):
        rpc._unpack = object()  # type: ignore[attr-defined]
    with pytest.raises(AttributeError, match=r"sandloom\.core\.rpc\.context\.time"):
        rpc.time = object()  # type: ignore[attr-defined]
    assert not hasattr(rpc, "_unpack")


def test_patching_where_the_name_is_looked_up_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proxy, "_RPC_RETRY_DELAY_SECONDS", 0)
    assert proxy._RPC_RETRY_DELAY_SECONDS == 0
    # Names the package owns itself (and its submodules) stay settable.
    monkeypatch.setattr(
        rpc, "install_service_rpc_routes", rpc.install_service_rpc_routes
    )
    assert rpc.server is server
