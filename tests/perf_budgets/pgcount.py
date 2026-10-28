"""PostgreSQL wire-protocol counting proxy (test-only, deterministic).

Sits between the application's engine and the test PostgreSQL and counts
client->server ROUND TRIPS exactly: every simple-protocol ``Query`` ('Q') and
every extended-protocol ``Sync`` ('S') is one round trip (the client then waits
for ``ReadyForQuery``). Wall-clock is meaningless on a loaded machine; round
trips are not — they are what a budget test pins.

Each round trip is recorded with the SQL it carried (from ``Query`` / ``Parse``
/ ``Bind``) so a failing budget prints WHAT was sent.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from dataclasses import dataclass, field

_SSL_REQUEST = 80877103
_GSS_REQUEST = 80877104


@dataclass
class RoundTrip:
    conn: int
    kind: str  # "Q" (simple query) or "S" (extended-protocol Sync)
    sql: list[str] = field(default_factory=list)

    def label(self) -> str:
        text = " | ".join(s for s in self.sql if s) or "<no statement>"
        return f"[c{self.conn} {self.kind}] {text[:200]}"


class PgCountingProxy:
    """Asyncio TCP proxy on 127.0.0.1:<ephemeral> forwarding to ``upstream``."""

    def __init__(self, host: str, port: int) -> None:
        self._upstream = (host, port)
        self._server: asyncio.base_events.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self.trace: list[RoundTrip] = []
        self.connections = 0
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # -- counting ---------------------------------------------------------------
    def reset(self) -> list[RoundTrip]:
        old, self.trace = self.trace, []
        return old

    @property
    def count(self) -> int:
        return len(self.trace)

    def report(self, trace: list[RoundTrip] | None = None) -> str:
        return "\n".join(rt.label() for rt in (self.trace if trace is None else trace))

    # -- plumbing ----------------------------------------------------------------
    async def _pump(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except Exception:  # noqa: BLE001, S110 - connection teardown
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _handle(
        self, creader: asyncio.StreamReader, cwriter: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        sreader, swriter = await asyncio.open_connection(*self._upstream)
        self.connections += 1
        conn_id = self.connections
        back = asyncio.ensure_future(self._pump(sreader, cwriter))
        buf = b""
        started = False
        group: list[str] = []
        statements: dict[bytes, str] = {}
        try:
            while data := await creader.read(65536):
                swriter.write(data)
                await swriter.drain()
                buf += data
                while True:
                    if not started:
                        if len(buf) < 8:
                            break
                        length = struct.unpack("!I", buf[:4])[0]
                        if len(buf) < length:
                            break
                        code = struct.unpack("!I", buf[4:8])[0]
                        buf = buf[length:]
                        if code not in (_SSL_REQUEST, _GSS_REQUEST):
                            started = True
                        continue
                    if len(buf) < 5:
                        break
                    kind = chr(buf[0])
                    length = struct.unpack("!I", buf[1:5])[0]
                    if len(buf) < 1 + length:
                        break
                    body = buf[5 : 1 + length]
                    buf = buf[1 + length :]
                    if kind == "Q":
                        sql = body[:-1].decode(errors="replace")
                        self.trace.append(RoundTrip(conn_id, "Q", [sql]))
                    elif kind == "P":
                        name, rest = body.split(b"\0", 1)
                        sql = rest.split(b"\0", 1)[0].decode(errors="replace")
                        statements[name] = sql
                        group.append("P:" + sql)
                    elif kind == "B":
                        _portal, rest = body.split(b"\0", 1)
                        name = rest.split(b"\0", 1)[0]
                        group.append("B:" + statements.get(name, name.decode()))
                    elif kind == "S":
                        self.trace.append(RoundTrip(conn_id, "S", group))
                        group = []
        except Exception:  # noqa: BLE001, S110 - connection teardown
            pass
        finally:
            with contextlib.suppress(Exception):
                swriter.close()
            back.cancel()
            if task is not None:
                self._tasks.discard(task)
