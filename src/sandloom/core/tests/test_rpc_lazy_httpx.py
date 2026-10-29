"""``import sandloom.core.di`` does not load httpx (and its CLI: click / pygments /
rich); the RPC client imports it on the first outbound call."""

from __future__ import annotations

import subprocess
import sys

import pytest

from sandloom.core.rpc import client as rpc_client


@pytest.mark.subprocess_probe
def test_importing_the_di_surface_does_not_load_httpx() -> None:
    code = (
        "import sys, sandloom.core.di, sandloom.core.rpc\n"
        "loaded = sorted(m for m in ('httpx', 'click', 'pygments', 'rich')"
        " if m in sys.modules)\n"
        "print(','.join(loaded))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip().splitlines()[-1:] in ([], [""])


def test_the_rpc_client_still_reaches_httpx_lazily() -> None:

    httpx = rpc_client._httpx()
    assert httpx.TimeoutException.__module__.startswith("httpx")
    assert rpc_client._transport().RpcResponseTooLarge.__mro__[1] is httpx.DecodingError
