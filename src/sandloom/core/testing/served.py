"""Race-free, identity-checked uvicorn subprocesses for multi-process tests.

The classic harness pattern — bind port 0, read the port, close the socket,
then start ``uvicorn --port N`` — is a TOCTOU race: between the close and the
child's ``bind`` any other process on the machine can take the port. The child
then dies with ``EADDRINUSE`` and the harness happily talks to the stranger
that now owns the port (a 404 from somebody else's server looks like "our
route does not exist").

This module closes both halves of that hole:

* **No TOCTOU.** :class:`ReservedPort` binds and LISTENs on a loopback port in
  the parent and keeps the socket open; :func:`spawn_served` passes that very
  socket to the child (``pass_fds``), which serves on it instead of binding.
  The port is never free between reservation and serving, and stays held by
  the parent across child restarts until :meth:`ReservedPort.close` — so a
  restarted child gets the same port back and no stranger can ever own it.
* **Identity on readiness.** Each spawn gets a random nonce; the child answers
  ``GET`` :data:`IDENTITY_PATH` with it (outermost ASGI wrapper, every other
  request passes through untouched). :func:`probe` reports "ready" only for
  the nonce of the process it spawned, fails fast when the child has exited
  (with its exit code), and fails loudly when something else answers.

Usage::

    port = ReservedPort()
    server = spawn_served(port, "sandloom.main:app", env=env, cwd=BACKEND,
                          stdout=log, stderr=subprocess.STDOUT)
    while not probe(server):          # raises SpawnedServerError on a dead
        time.sleep(0.2)               # child or a foreign answer
    ...
    server.stop(); port.close()

The child entrypoint is ``python -m sandloom.core.testing.served <app> --fd N``.
"""

from __future__ import annotations

import argparse
import http.client
import os
import secrets
import socket
import subprocess
import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import uvicorn
from uvicorn.importer import import_from_string

__all__ = [
    "IDENTITY_PATH",
    "NONCE_ENV",
    "ReservedPort",
    "SpawnedServer",
    "SpawnedServerError",
    "probe",
    "spawn_served",
]

#: Answered by the child's outermost wrapper with its spawn nonce.
IDENTITY_PATH = "/__sandloom_spawn_identity__"
#: Environment variable carrying the nonce into the child.
NONCE_ENV = "SANDLOOM_SPAWN_NONCE"


class SpawnedServerError(AssertionError):
    """The spawned server exited, or its port is answered by another process."""


class ReservedPort:
    """A loopback port held by a listening socket owned by the test process.

    While open, no other process can bind the port; children inherit the very
    socket. A test that must make the port REFUSE connections for a while (a
    "service down" window) :meth:`close` it and :meth:`reopen` the same
    port afterwards — that re-bind fails loudly (``OSError``) if another
    process took the port meanwhile, it never silently shares it.
    """

    def __init__(self, host: str = "127.0.0.1", backlog: int = 128) -> None:
        self.host = host
        self._backlog = backlog
        self._sock: socket.socket | None = None
        self.port: int = self._listen(0)

    def _listen(self, port: int) -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # TIME_WAIT leftovers of a previous child must not block a reopen;
            # SO_REUSEADDR never permits binding over a LIVE listener.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, port))
            sock.listen(self._backlog)
        except BaseException:
            sock.close()
            raise
        self._sock = sock
        return int(sock.getsockname()[1])

    def reopen(self) -> None:
        """Re-reserve the SAME port after :meth:`close` (no-op while open)."""
        if self._sock is None:
            try:
                self._listen(self.port)
            except OSError as exc:
                raise SpawnedServerError(
                    f"cannot re-reserve {self.url}: {exc} (another process "
                    "took the port while it was released)"
                ) from exc

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def fileno(self) -> int:
        if self._sock is None:
            raise RuntimeError(f"reserved port {self.port} was already closed")
        return self._sock.fileno()

    @property
    def closed(self) -> bool:
        return self._sock is None

    def close(self) -> None:
        """Release the port (idempotent). Do it after the last child stopped."""
        if self._sock is not None:
            self._sock.close()
            self._sock = None


@dataclass
class SpawnedServer:
    """A child serving on a :class:`ReservedPort`, identified by its nonce."""

    proc: subprocess.Popen[bytes]
    url: str
    nonce: str
    label: str = ""

    def stop(self, timeout: float = 10.0) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:  # pragma: no cover
                self.proc.kill()
                self.proc.wait(timeout=5)


def spawn_served(
    port: ReservedPort,
    app: str,
    *,
    env: dict[str, str],
    log_level: str = "warning",
    label: str = "",
    **popen_kwargs: Any,
) -> SpawnedServer:
    """Start ``app`` (``"module:attr"``) in a child serving on ``port``'s socket."""
    nonce = secrets.token_hex(16)
    fd = port.fileno()
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, test harness
        [
            sys.executable,
            "-m",
            "sandloom.core.testing.served",
            app,
            "--fd",
            str(fd),
            "--log-level",
            log_level,
        ],
        env={**env, NONCE_ENV: nonce},
        pass_fds=(fd,),
        **popen_kwargs,
    )
    return SpawnedServer(proc=proc, url=port.url, nonce=nonce, label=label or app)


def probe(server: SpawnedServer, timeout: float = 1.0) -> bool:
    """One readiness check: ``True`` once THIS spawn answers with its nonce.

    ``False`` while it is not serving yet. Raises :class:`SpawnedServerError`
    when the child has exited (e.g. a startup or bind failure) or when the
    port answers with anything but this spawn's nonce — a different process
    is serving there, and no request must be sent to it.
    """
    code = server.proc.poll()
    if code is not None:
        raise SpawnedServerError(
            f"{server.label} ({server.url}) exited with code {code} before it "
            "was ready (startup failure; see its log)"
        )
    address = urlsplit(server.url)
    if address.scheme != "http" or address.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise SpawnedServerError("readiness probes require a loopback HTTP server")
    # This is always plain HTTP to the socket reserved by the harness. A fresh
    # HTTPX client loaded the CA bundle on every poll, even for plain HTTP.
    # A direct connection also ignores user proxy settings and never redirects.
    connection = http.client.HTTPConnection(
        address.hostname, address.port, timeout=timeout
    )
    try:
        connection.request("GET", IDENTITY_PATH)
        response = connection.getresponse()
        status = response.status
        body = response.read(1024).decode("utf-8", errors="replace")
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()
    if status == 200 and body == server.nonce:
        return True
    raise SpawnedServerError(
        f"{server.label}: {server.url} is answered by a different process "
        f"(GET {IDENTITY_PATH} -> {status} {body[:200]!r}); "
        "refusing to talk to it"
    )


# ---------------------------------------------------------------------------
# child side
# ---------------------------------------------------------------------------


class _IdentityApp:
    """Outermost ASGI wrapper: answers the identity probe, passes the rest."""

    def __init__(self, app: Any, nonce: str) -> None:
        self.app = app
        self.nonce = nonce.encode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if (
            scope["type"] == "http"
            and scope["path"] == IDENTITY_PATH
            and scope["method"] == "GET"
        ):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"text/plain"),
                        (b"content-length", str(len(self.nonce)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": self.nonce})
            return
        await self.app(scope, receive, send)


def _main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m sandloom.core.testing.served")
    parser.add_argument("app")
    parser.add_argument("--fd", type=int, required=True)
    parser.add_argument("--log-level", default="warning")
    args = parser.parse_args(argv)

    nonce = os.environ.get(NONCE_ENV, "")
    if not nonce:
        sys.exit(f"{NONCE_ENV} is not set")
    # ``socket(fileno=)`` detects the family (uvicorn's own ``--fd`` assumes
    # AF_UNIX) — the inherited listening TCP socket is served as is.
    sock = socket.socket(fileno=args.fd)
    app = _IdentityApp(import_from_string(args.app), nonce)
    config = uvicorn.Config(app, log_level=args.log_level)
    server = uvicorn.Server(config)
    server.run(sockets=[sock])


if __name__ == "__main__":  # pragma: no cover - subprocess entrypoint
    _main()
