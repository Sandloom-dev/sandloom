"""Race-free, identity-checked test subprocess servers (``sandloom.core.testing.served``).

Regression for a flaky split-conformance run: the harness picked a "free"
port, closed it, and the child bound it later; another process took the port
in between, uvicorn died with ``EADDRINUSE`` and the readiness probe plus the
whole request script talked to the stranger (every route "404 Not Found").
"""

from __future__ import annotations

import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from sandloom.core.testing.served import (
    IDENTITY_PATH,
    ReservedPort,
    SpawnedServer,
    SpawnedServerError,
    probe,
    spawn_served,
)

APP = "sandloom.core.tests.test_served_app:app"
SRC = str(Path(__file__).resolve().parents[3])


async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """The child's application: says who it is on every HTTP path."""
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    body = f"child {os.getpid()} {scope['path']}".encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([SRC, env.get("PYTHONPATH", "")])
    return env


def _wait(server: SpawnedServer, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe(server):
            return
        time.sleep(0.05)
    raise AssertionError("child did not start")


class _Stranger(BaseHTTPRequestHandler):
    """Somebody else's server: answers everything with 404 Not Found."""

    def do_GET(self) -> None:  # http.server API
        body = b'{"detail":"Not Found"}'
        self.send_response(404)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def stranger() -> Iterator[HTTPServer]:
    server = HTTPServer(("127.0.0.1", 0), _Stranger)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _occupy(port: int) -> socket.socket:
    """Try to take ``port`` the way an unrelated server would."""
    sock = socket.socket()
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
    except BaseException:
        sock.close()
        raise
    return sock


def test_reserved_port_cannot_be_taken_before_the_child_serves() -> None:
    port = ReservedPort()
    try:
        # The window that used to exist between "pick a port" and the child's
        # bind: an occupant now cannot get the port at all.
        with pytest.raises(OSError):
            _occupy(port.port)
        server = spawn_served(port, APP, env=_env(), label="child")
        try:
            _wait(server)
            resp = httpx.get(f"{port.url}/hello", timeout=5)
            assert resp.status_code == 200
            assert resp.text == f"child {server.proc.pid} /hello"
        finally:
            server.stop()
    finally:
        port.close()


def test_identity_path_is_answered_only_with_this_spawns_nonce() -> None:
    port = ReservedPort()
    try:
        first = spawn_served(port, APP, env=_env(), label="first")
        try:
            _wait(first)
            stale = SpawnedServer(first.proc, first.url, "not-" + first.nonce, "x")
            with pytest.raises(SpawnedServerError, match="different process"):
                probe(stale)
            assert httpx.get(f"{port.url}{IDENTITY_PATH}").text == first.nonce
        finally:
            first.stop()
    finally:
        port.close()


def test_occupied_port_legacy_spawn_is_never_reported_ready(
    stranger: HTTPServer, tmp_path: Path
) -> None:
    """The CI failure, deterministically: the port is taken before the child
    binds it. The old readiness probe accepted the stranger; ``probe`` fails
    clearly instead — while the child is alive and once it has exited."""
    port = int(stranger.server_address[1])
    # Old harness readiness: "anything answers" — the stranger passes it.
    assert httpx.get(f"http://127.0.0.1:{port}/health/simple").status_code == 404

    log = tmp_path / "child.log"
    with log.open("wb") as out:
        legacy = subprocess.Popen(  # the old `uvicorn --port N` spawn
            [
                sys.executable,
                "-m",
                "uvicorn",
                APP,
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            env=_env(),
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    server = SpawnedServer(legacy, f"http://127.0.0.1:{port}", "nonce", "legacy")
    try:
        # Alive or already dead: never "ready", never silently the stranger.
        with pytest.raises(SpawnedServerError) as alive_or_dead:
            probe(server)
        assert str(alive_or_dead.value)
        legacy.wait(timeout=30)
        with pytest.raises(SpawnedServerError, match="exited with code"):
            probe(server)
        assert "address already in use" in log.read_text().lower()
    finally:
        server.stop()


def test_probe_rejects_a_stranger_while_the_child_is_alive(
    stranger: HTTPServer,
) -> None:
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    server = SpawnedServer(
        sleeper, f"http://127.0.0.1:{stranger.server_address[1]}", "nonce", "child"
    )
    try:
        with pytest.raises(SpawnedServerError, match="different process.*404"):
            probe(server)
    finally:
        server.stop()


def test_plain_http_probe_does_not_build_tls_contexts_or_use_proxy_env(
    stranger: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A frequent localhost readiness poll must not load a CA bundle each time."""
    from unittest.mock import Mock

    proc = Mock()
    proc.poll.return_value = None
    server = SpawnedServer(
        proc, f"http://127.0.0.1:{stranger.server_address[1]}", "nonce", "probe"
    )

    def unexpected_tls(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("plain HTTP readiness probe constructed a TLS context")

    monkeypatch.setattr(ssl, "create_default_context", unexpected_tls)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    # Reaching the real stranger proves that a proxy was not consulted.
    with pytest.raises(SpawnedServerError, match="different process"):
        probe(server)


@pytest.mark.parametrize("url", ["https://127.0.0.1:1", "http://example.com:80"])
def test_readiness_probe_only_accepts_loopback_http(url: str) -> None:
    from unittest.mock import Mock

    proc = Mock()
    proc.poll.return_value = None
    with pytest.raises(SpawnedServerError, match="loopback HTTP"):
        probe(SpawnedServer(proc, url, "nonce", "probe"))


def test_reopen_after_release_refuses_a_port_taken_meanwhile() -> None:
    port = ReservedPort()
    number = port.port
    port.close()
    port.reopen()  # nobody took it: the same port again
    assert port.port == number and not port.closed
    port.close()
    occupant = _occupy(number)
    try:
        with pytest.raises(SpawnedServerError, match="re-reserve"):
            port.reopen()
    finally:
        occupant.close()
        port.close()


def test_a_child_that_dies_on_startup_fails_fast(tmp_path: Path) -> None:
    port = ReservedPort()
    try:
        with (tmp_path / "log").open("wb") as out:
            server = spawn_served(
                port,
                "sandloom.core.tests.no_such_module:app",
                env=_env(),
                label="broken",
                stdout=out,
                stderr=subprocess.STDOUT,
            )
        server.proc.wait(timeout=30)
        with pytest.raises(SpawnedServerError, match="broken .* exited with code"):
            probe(server)
    finally:
        port.close()
