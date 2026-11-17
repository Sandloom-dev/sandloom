"""The backing server of the standard stack, as its driver package describes it.

``sandloom new`` and ``sandloom dev`` are products of the kernel CLI for one
stack: PostgreSQL and a keyed server that holds the cache, the broadcast, the
security store, the lease, the event streams and the task-queue broker, one
numbered database each. The CLI knows how to start, probe, share and empty such
a server, and how to render a compose file around it, but not what the server
is: the package of the driver the stack selects says so, by registering an
object with the members of :class:`StackService` in the entry-point group
:data:`GROUP` (``"redis" = "sandloom_redis.stack:STACK_SERVICE"``). The kernel
holds no client, no image name and no URL layout of it.

A driver package imports what it needs from ``sandloom.driverkit.stack_service``,
the re-export of this module.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from functools import cache
from importlib import metadata
from typing import Protocol

#: Entry-point group: ``"<driver>" = "package.stack:STACK_SERVICE"``.
GROUP = "sandloom.stack"
#: The driver the standard stack runs its keyed instances on (the one the
#: ``DEFAULT_INFRA`` of a generated app selects), the name its package registers
#: its :class:`StackService` under.
STANDARD_DRIVER = "redis"


class StackServiceError(ValueError):
    """No package describes the standard stack's backing server."""


class KeyedServer(Protocol):
    """One server of the stack: where it listens and the url of each of its
    numbered databases (``url(index)``; no index: the server alone)."""

    @property
    def host(self) -> str: ...

    @property
    def port(self) -> int: ...

    @property
    def password(self) -> str: ...

    @property
    def address(self) -> str: ...

    def url(self, index: int | None = None) -> str: ...


class StackService(Protocol):
    """What ``sandloom dev`` and the scaffold of ``sandloom new`` /
    ``sandloom add deploy`` ask of the stack's backing server.

    Names and ports are the compose project's (``name``, ``test_name``: the dev
    and the disposable test service, with the host-port variable each reads and
    the port inside the container). ``label`` and ``start_command`` are what a
    person reads. The text members are the compose YAML of the server for each
    generated file; the kernel places them and writes no line of them."""

    name: str
    test_name: str
    label: str
    port_var: str
    test_port_var: str
    container_port: int
    #: The port of a server a person runs by hand, and the command that starts it.
    local_port: int
    start_command: str
    #: How many numbered databases the dev server has, and the two a test run
    #: starts from (the cache's, the persistent instances').
    index_count: int
    test_cache_index: int
    test_broker_index: int

    # ------------------------------------------------------------ dev tooling
    def server(self, host: str, port: int) -> KeyedServer:
        """The server at ``host``:``port`` (no password: the compose one)."""
        ...

    def parse(self, url: str) -> KeyedServer:
        """The server of ``url``, or ``ValueError`` (text without the password)
        for one that cannot be carried end to end (TLS, a user, options)."""
        ...

    def local_url(self, host: str) -> str:
        """The url of a server on the default port of ``host``."""
        ...

    def problem(self, server: KeyedServer) -> str | None:
        """``None`` when the server answers, else why not (one line)."""
        ...

    def clients(
        self, server: KeyedServer, indexes: Collection[int] | None = None
    ) -> int:
        """Other clients connected to the server (to ``indexes`` when given)."""
        ...

    def empty_indexes(
        self, server: KeyedServer, candidates: Iterable[int], *, want: int = 2
    ) -> list[int]:
        """The first ``want`` of ``candidates`` that hold no key."""
        ...

    def flush(self, server: KeyedServer, indexes: Iterable[int]) -> None:
        """Empty each of ``indexes``."""
        ...

    def errors(self) -> tuple[type[BaseException], ...]:
        """What the client raises when the server is down or refuses us."""
        ...

    # ---------------------------------------------------------------- scaffold
    def dev_compose(self, port_hint: str, *, test: bool) -> str:
        """The service block of ``deploy/docker-compose.dev.yml`` (the disposable
        test server when ``test``); ``port_hint`` is what a missing port says."""
        ...

    #: The block of the monolith profile's ``deploy/docker-compose.yml`` and
    #: the name of the volume it mounts.
    prod_compose: str
    prod_volume: str
    #: The block of a CI job's ``services:``.
    ci_service: str
    #: The microservices profile (``deploy/compose/microservices.yml``): the
    #: shared ``INFRA`` line, the server block with the opt-in second one, the
    #: volume entries.
    microservices_infra: str
    microservices_service: str
    microservices_volumes: str


@cache
def _discover(driver: str) -> StackService:
    for entry in metadata.entry_points().select(group=GROUP):
        if entry.name == driver:
            found: StackService = entry.load()
            return found
    raise StackServiceError(
        f"no installed package describes the backing server of the standard "
        f"stack ({driver!r}, entry-point group {GROUP!r}): install "
        "`sandloom[redis]`"
    )


def load(driver: str = STANDARD_DRIVER) -> StackService:
    """The :class:`StackService` registered for ``driver`` (discovered once)."""
    return _discover(driver)


__all__ = [
    "GROUP",
    "STANDARD_DRIVER",
    "KeyedServer",
    "StackService",
    "StackServiceError",
    "load",
]
