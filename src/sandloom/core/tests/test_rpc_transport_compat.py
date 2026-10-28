"""aiohttp-internals probe behind ``RPC_TRANSPORT=aiohttp`` (no database).

Pins: the probe passes on the installed (verified) aiohttp; the installed
version is inside the verified range and that range is what ``pyproject.toml``
pins; a changed internal (a name, a kind, or the idle-connection guard's
BEHAVIOUR) is detected and turns into one warning + a metric + the httpx
client — never a failed request.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import sys
import tomllib
from collections import namedtuple
from collections.abc import Iterator
from importlib import metadata
from pathlib import Path

import httpx
import pytest
from packaging.version import Version
from prometheus_client import REGISTRY

from sandloom.core import rpc, rpc_transport, rpc_transport_compat
from sandloom.core.config.settings import settings
from sandloom.core.contract_testing import rpc_response_headers, rpc_settings
from sandloom.core.observability import metrics  # noqa: F401 - registers the series
from sandloom.core.rpc import client as rpc_client
from sandloom.core.rpc import wire as rpc_wire
from sandloom.core.rpc_transport import AiohttpRpcClient
from sandloom.core.rpc_transport_compat import (
    REASON_IMPORT,
    REASON_INTERNALS,
    REASON_SELFTEST,
    VERIFIED_AIOHTTP_RANGE,
)

BACKEND = Path(__file__).resolve().parents[4]


@pytest.fixture(autouse=True)
def _fresh_probes() -> Iterator[None]:
    rpc_transport_compat.reset_transport_probes()
    yield
    rpc_transport_compat.reset_transport_probes()


def _fallbacks(reason: str) -> float:
    value = REGISTRY.get_sample_value(
        "sandloom_rpc_transport_fallback_total", {"reason": reason}
    )
    return float(value or 0.0)


def _unguarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """aiohttp no longer builds connections with the guarded protocol (as if
    ``_factory`` were renamed): names still check out, behaviour does not."""
    import aiohttp

    monkeypatch.setattr(
        rpc_transport, "_observed_connector_class", lambda: aiohttp.TCPConnector
    )


# --- the installed aiohttp ------------------------------------------------------


def test_static_probe_passes_on_the_installed_aiohttp() -> None:
    result = rpc_transport_compat.static_probe()
    assert result.ok, result.problems


@pytest.mark.asyncio
async def test_full_probe_with_the_self_test_passes_on_the_installed_aiohttp() -> None:
    result = await rpc_transport_compat.full_probe()
    assert result.ok, result.problems
    assert rpc_transport_compat.aiohttp_usable()


def test_installed_aiohttp_is_inside_the_verified_range() -> None:
    low, high = VERIFIED_AIOHTTP_RANGE
    installed = Version(metadata.version("aiohttp"))
    assert Version(low) <= installed < Version(high), (
        f"aiohttp {installed} is outside the range the RPC transport was "
        f"verified for [{low}, {high}); follow docs/operations/operator-reference.md 'Upgrading "
        "aiohttp' and then widen VERIFIED_AIOHTTP_RANGE and pyproject.toml together"
    )


@pytest.mark.parametrize(
    "pyproject",
    [
        BACKEND / "pyproject.toml",
        BACKEND / "packages/sandloom/pyproject.toml",
    ],
    ids=["backend", "sandloom"],
)
def test_pyproject_pins_exactly_the_verified_range(pyproject: Path) -> None:
    if not pyproject.exists():
        pytest.skip("source tree not available")
    project = tomllib.loads(pyproject.read_text())["project"]
    deps = project["dependencies"]
    if pyproject == BACKEND / "pyproject.toml":
        # The contributor environment installs the distribution that owns drivers.
        assert any(d.startswith("sandloom[") for d in deps), deps
        kernel = BACKEND / "packages/sandloom/pyproject.toml"
        deps = tomllib.loads(kernel.read_text())["project"]["dependencies"]
    low, high = VERIFIED_AIOHTTP_RANGE
    spec = next(d for d in deps if re.match(r"aiohttp(\[|>|=|<|$)", d))
    assert spec.endswith(f">={low},<{high}"), (pyproject, spec)


# --- detection ------------------------------------------------------------------


def test_a_missing_private_method_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    from aiohttp.client_proto import ResponseHandler

    monkeypatch.delattr(ResponseHandler, "force_close")
    result = rpc_transport_compat.static_probe()
    assert not result.ok
    assert result.reason == REASON_INTERNALS
    assert any("force_close() is missing" in p for p in result.problems)


def test_a_property_turned_attribute_is_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiohttp.client_proto import ResponseHandler

    monkeypatch.setattr(ResponseHandler, "should_close", False)
    result = rpc_transport_compat.static_probe()
    assert any("should_close is not a property" in p for p in result.problems)


def test_a_new_connection_key_field_is_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import aiohttp.client_reqrep

    fields = (*aiohttp.client_reqrep.ConnectionKey._fields, "tenant")
    monkeypatch.setattr(
        aiohttp.client_reqrep, "ConnectionKey", namedtuple("ConnectionKey", fields)
    )
    result = rpc_transport_compat.static_probe()
    assert any("unknown fields ['tenant']" in p for p in result.problems)


def test_a_missing_aiohttp_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "aiohttp", None)
    result = rpc_transport_compat.static_probe()
    assert not result.ok
    assert result.reason == REASON_IMPORT


@pytest.mark.asyncio
async def test_a_changed_instance_attribute_is_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import aiohttp

    original = aiohttp.TCPConnector.__init__

    @functools.wraps(original)
    def init(self: aiohttp.TCPConnector, *args: object, **kwargs: object) -> None:
        original(self, *args, **kwargs)  # type: ignore[arg-type]
        self._acquired = list(self._acquired)  # type: ignore[assignment]

    monkeypatch.setattr(aiohttp.TCPConnector, "__init__", init)
    result = await rpc_transport_compat.full_probe()
    assert result.reason == REASON_INTERNALS
    assert any("_acquired is not a set" in p for p in result.problems)


@pytest.mark.asyncio
async def test_a_guard_that_no_longer_engages_is_caught_by_the_self_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every name still exists, but unsolicited bytes on an idle connection
    reach the next call: only the behavioural self-test sees it."""
    _unguarded(monkeypatch)
    assert rpc_transport_compat.static_probe().ok
    result = await rpc_transport_compat.full_probe()
    assert not result.ok
    assert result.reason == REASON_SELFTEST
    assert any("instead of its own" in p for p in result.problems), result.problems


# --- fallback -------------------------------------------------------------------


def test_a_failing_probe_warns_once_counts_and_falls_back_to_httpx(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from aiohttp.client_proto import ResponseHandler

    monkeypatch.delattr(ResponseHandler, "force_close")
    monkeypatch.setattr(settings, "rpc_transport", "aiohttp")
    monkeypatch.setattr(rpc_client, "_rpc_client_instance", None)
    monkeypatch.setattr(rpc_client, "_rpc_client_kind", None)
    before = _fallbacks(REASON_INTERNALS)
    with caplog.at_level(logging.WARNING, logger="sandloom.core.rpc_transport"):
        client = rpc_client._rpc_client()
        assert isinstance(client, httpx.AsyncClient)
        assert rpc_client._rpc_client() is client  # cached: not rebuilt per call
        rpc_transport_compat.static_probe()
    warnings = [r for r in caplog.records if "falls back to httpx" in r.getMessage()]
    assert len(warnings) == 1
    assert _fallbacks(REASON_INTERNALS) == before + 1


@pytest.mark.asyncio
async def test_startup_probe_failure_rebuilds_the_shared_client_on_httpx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await rpc.close_rpc_client()
    monkeypatch.setattr(settings, "rpc_transport", "aiohttp")
    monkeypatch.setattr(
        settings, "service_bindings", {"x.y.v1": {"base_url": "http://127.0.0.1:1"}}
    )
    assert isinstance(rpc_client._rpc_client(), AiohttpRpcClient)  # static probe: ok
    _unguarded(monkeypatch)
    before = _fallbacks(REASON_SELFTEST)
    await rpc.check_rpc_transport()
    assert _fallbacks(REASON_SELFTEST) == before + 1
    assert isinstance(rpc_client._rpc_client(), httpx.AsyncClient)
    await rpc.close_rpc_client()


@pytest.mark.asyncio
async def test_startup_probe_is_skipped_without_outbound_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "rpc_transport", "aiohttp")
    monkeypatch.setattr(settings, "service_bindings", {})
    calls: list[int] = []

    async def probe() -> rpc_transport_compat.TransportProbe:
        calls.append(1)
        return rpc_transport_compat.TransportProbe(True)

    monkeypatch.setattr(rpc_transport_compat, "full_probe", probe)
    await rpc.check_rpc_transport()
    assert calls == []


@pytest.mark.asyncio
async def test_the_fallback_client_serves_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a fallback, outbound calls keep working (on httpx)."""

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        length = int(re.search(rb"(?i)content-length:\s*(\d+)", head).group(1))  # type: ignore[union-attr]
        await reader.readexactly(length)
        sent = httpx.Headers(
            [
                (name.strip(), value.strip())
                for name, _, value in (
                    line.decode("latin-1").partition(":")
                    for line in head.split(b"\r\n")[1:]
                    if line
                )
            ]
        )
        body = rpc_wire._pack({"ok": True, "result": "pong"})
        # Answered as a keyed provider (``svc``): the proxy accepts only that.
        bound = rpc_response_headers(settings, sent, 200, body)
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            + b"".join(f"{k}: {v}\r\n".encode() for k, v in bound.items())
            + b"content-length: %d\r\n\r\n" % len(body)
            + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    from aiohttp.client_proto import ResponseHandler

    monkeypatch.delattr(ResponseHandler, "force_close")
    monkeypatch.setattr(settings, "rpc_transport", "aiohttp")
    for name, value in rpc_settings("svc", allowed_calls={"svc": ["demo.v1"]}).items():
        monkeypatch.setattr(settings, name, value)
    await rpc.close_rpc_client()
    try:
        proxy = rpc.RemoteServiceProxy(
            service_name="demo.v1",
            binding={"base_url": f"http://127.0.0.1:{port}", "service_id": "svc"},
            default_timeout_seconds=2,
        )
        assert await proxy.ping() == "pong"
        assert isinstance(rpc_client._rpc_client(), httpx.AsyncClient)
    finally:
        await rpc.close_rpc_client()
        server.close()
        await server.wait_closed()
