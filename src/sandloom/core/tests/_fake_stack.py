"""An in-memory :class:`~sandloom.core.cli.stack_service.StackService`.

``sandloom dev`` and the scaffold of ``sandloom new`` ask the installed driver
package for the stack's keyed server. The kernel's tests run on every lane,
also where that package is not installed, so they ask this stand-in: a keyed
server of numbered databases held in a dict, with the answers a real one gives
(``problem``, ``clients``, ``empty_indexes``, ``flush``) and the refusals a
driver's ``parse`` makes for a url it cannot carry. The real server and its
client: ``packages/sandloom-redis/tests/test_stack_service.py``.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit

import pytest

from sandloom.core.cli import stack_service as stack
from sandloom.core.cli.dev.services import redact

#: The databases of the stand-in server (a real one's own setting).
DATABASES = 256


class FakeServerDown(OSError):
    """What the stand-in's client raises when the server is stopped."""


@dataclass(frozen=True)
class FakeServer:
    host: str
    port: int
    password: str = ""

    def url(self, index: int | None = None) -> str:
        auth = f":{quote(self.password, safe='')}@" if self.password else ""
        tail = "" if index is None else f"/{index}"
        return f"redis://{auth}{self.host}:{self.port}{tail}"

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass
class FakeStack:
    """Names and ports of the standard stack's server, a dict for its data."""

    name: str = "redis"
    test_name: str = "redis_test"
    label: str = "Redis"
    port_var: str = "SANDLOOM_DEV_REDIS_PORT"
    test_port_var: str = "SANDLOOM_DEV_TEST_REDIS_PORT"
    container_port: int = 6379
    local_port: int = 6379
    start_command: str = "brew services start redis"
    index_count: int = DATABASES
    test_cache_index: int = 15
    test_broker_index: int = 14
    prod_compose: str = ""
    prod_volume: str = ""
    ci_service: str = ""
    ci_port: int = 6380
    microservices_infra: str = ""
    microservices_service: str = ""
    microservices_volumes: str = ""
    #: ``{index: {key: value}}``.
    keys: dict[int, dict[str, str]] = field(default_factory=dict)
    #: Why ``problem`` says the server does not answer (``None``: it answers).
    down: str | None = None

    def server(self, host: str, port: int) -> FakeServer:
        return FakeServer(host, port)

    def parse(self, url: str) -> FakeServer:
        parts = urlsplit(url.strip())
        if parts.scheme == "rediss":
            raise ValueError("rediss:// (TLS) is not supported yet")
        if parts.scheme != "redis" or not parts.hostname:
            raise ValueError(f"not a keyed-server URL: {redact(url)}")
        if parts.username or parts.query:
            raise ValueError("a user or URL options are not supported yet")
        return FakeServer(
            parts.hostname, parts.port or self.local_port, unquote(parts.password or "")
        )

    def local_url(self, host: str) -> str:
        return f"redis://{host}:{self.local_port}"

    def problem(self, server: stack.KeyedServer) -> str | None:
        return self.down

    def clients(
        self, server: stack.KeyedServer, indexes: Collection[int] | None = None
    ) -> int:
        return 0

    def empty_indexes(
        self, server: stack.KeyedServer, candidates: Iterable[int], *, want: int = 2
    ) -> list[int]:
        found: list[int] = []
        for index in candidates:
            if index >= self.index_count:
                break
            if not self.keys.get(index):
                found.append(index)
                if len(found) == want:
                    break
        return found

    def flush(self, server: stack.KeyedServer, indexes: Iterable[int]) -> None:
        for index in indexes:
            self.keys.pop(index, None)

    def errors(self) -> tuple[type[BaseException], ...]:
        return (FakeServerDown,)

    def dev_compose(self, port_hint: str, *, test: bool) -> str:
        return f"  {self.test_name if test else self.name}: {{}}"


def install_fake_stack(monkeypatch: pytest.MonkeyPatch) -> FakeStack:
    """Make :func:`sandloom.core.cli.stack_service.load` return a fresh
    :class:`FakeStack` for the test."""
    fake = FakeStack()
    monkeypatch.setattr(stack, "_discover", lambda driver: fake)
    return fake
