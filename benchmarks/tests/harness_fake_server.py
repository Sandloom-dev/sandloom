"""A misbehaving HTTP/1.1 keep-alive server for the harness negative controls.

Run as its own PROCESS (``python harness_fake_server.py``; prints its port on
the first stdout line) so the client under test shares neither the event loop
nor the GIL with it. Behaviour is chosen by the request path:

======================  =================================================
``/ok``                 200, Content-Length body
``/chunked``            200, chunked body (two chunks)
``/created``            201
``/redirect``           307, empty body
``/e500`` ``/e503``     500 / 503
``/slow``               sleeps ``SLOW_S`` (2 s), then 200 — a client timeout
``/drop``               closes the connection without answering
``/half``               sends half a status line, then closes
======================  =================================================
"""

from __future__ import annotations

import asyncio
import sys

SLOW_S = 2.0


def _resp(status: str, body: bytes = b"", extra: str = "") -> bytes:
    return (
        f"HTTP/1.1 {status}\r\ncontent-type: text/plain\r\n"
        f"content-length: {len(body)}\r\n{extra}\r\n"
    ).encode() + body


FIXED = {
    "/ok": _resp("200 OK", b'{"ok":true}'),
    "/created": _resp("201 Created", b'{"id":1}'),
    "/redirect": _resp("307 Temporary Redirect", extra="location: /ok\r\n"),
    "/e500": _resp("500 Internal Server Error", b"boom"),
    "/e503": _resp("503 Service Unavailable", b"busy"),
    "/chunked": (
        b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n"
        b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
    ),
}


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            path = lines[0].split(" ")[1].split("?")[0]
            length = 0
            for h in lines[1:]:
                if h.lower().startswith("content-length:"):
                    length = int(h.split(":", 1)[1])
            if length:
                await reader.readexactly(length)
            if path in FIXED:
                writer.write(FIXED[path])
            elif path == "/slow":
                await asyncio.sleep(SLOW_S)
                writer.write(FIXED["/ok"])
            elif path == "/drop":
                writer.close()
                return
            elif path == "/half":
                writer.write(b"HTTP/1.1 2")
                await writer.drain()
                writer.close()
                return
            else:
                writer.write(_resp("404 Not Found"))
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        writer.close()


async def main() -> None:
    server = await asyncio.start_server(handle, "127.0.0.1", 0, backlog=1024)
    print(server.sockets[0].getsockname()[1], flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        import uvloop

        run = uvloop.run
    except ImportError:
        run = asyncio.run
    try:
        run(main())
    except KeyboardInterrupt:
        sys.exit(0)
