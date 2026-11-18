"""The Redis server of the standard stack, for ``sandloom dev`` and the scaffold.

``sandloom dev`` starts, probes, shares and empties the stack's keyed server, and
``sandloom new`` / ``sandloom add deploy`` render compose files around it. The
kernel CLI does that through :class:`~sandloom.driverkit.stack_service.StackService`
and knows nothing of Redis; this module is the Redis half: the server and its
urls (:class:`RedisServer`), the checks through the client, and the compose text.
The object :data:`STACK_SERVICE` is registered in the entry-point group
``sandloom.stack`` by this package's ``pyproject.toml``.

The compose text is the text the kernel rendered before it moved here: the
snapshot ``tests/integration/with_batteries/test_scaffold_standard_stack_snapshot.py``
holds the output of ``sandloom new`` and ``sandloom add deploy`` byte for byte.
"""

# ruff: noqa: E501 - the compose text keeps the line lengths of the files it renders

from __future__ import annotations

import contextlib
import socket
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from urllib.parse import quote, unquote

import redis

from sandloom.driverkit.stack_service import KeyedServer
from sandloom.driverkit.url_split import redact, split_url

__all__ = ["STACK_SERVICE", "RedisServer", "RedisStackService"]

_DEFAULT_PORT = 6379


def _bracketed(host: str) -> str:
    """``host`` as it stands in a URL: an IPv6 literal (``::1``; ``urlsplit``
    hands it back without its brackets) gets them again."""
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


@dataclass(frozen=True)
class RedisServer:
    """A Redis server: host, port and the password of its default user."""

    host: str
    port: int
    password: str = ""

    def url(self, index: int | None = None) -> str:
        auth = f":{quote(self.password, safe='')}@" if self.password else ""
        tail = "" if index is None else f"/{index}"
        return f"redis://{auth}{self.address}{tail}"

    @classmethod
    def from_url(cls, url: str) -> RedisServer:
        parts = split_url(url.strip())
        if parts.scheme == "rediss":
            # TLS is not carried end to end yet (the URLs written, the checks'
            # client): refuse rather than send AUTH in cleartext.
            raise ValueError(
                f"rediss:// (TLS) is not supported yet: {redact(url)}. Use a local "
                "Redis without TLS (redis://127.0.0.1:6379), or --services docker"
            )
        if parts.scheme != "redis" or not parts.hostname:
            raise ValueError(
                f"not a Redis URL: {redact(url)} (for example redis://127.0.0.1:6379)"
            )
        if parts.username or parts.query:
            # Only a password is carried: an ACL user (or `?…` options) would
            # be dropped silently.
            raise ValueError(
                f"a Redis ACL user or URL options are not supported yet: "
                f"{redact(url)}. Use redis://:password@host:port (the default "
                "user), or --services docker"
            )
        return cls(
            parts.hostname,
            parts.port or _DEFAULT_PORT,
            unquote(parts.password or ""),
        )

    @property
    def address(self) -> str:
        return f"{_bracketed(self.host)}:{self.port}"


def _connect(server: KeyedServer, db: int = 0) -> redis.Redis[bytes]:
    return redis.Redis(
        host=server.host,
        port=server.port,
        db=db,
        password=server.password or None,
        socket_connect_timeout=3,
        socket_timeout=5,
    )


def _tcp_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


_DEV_BLOCK = """\
  {name}:
    image: redis:7-alpine
    command: ["redis-server", "--databases", "1024"]
    ports: ["127.0.0.1:${{{var}:?{hint}}}:6379"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 2s
      timeout: 2s
      retries: 20"""

_PROD_BLOCK = """\
  redis:
    image: redis:7-alpine
    command: ["redis-server", "--appendonly", "yes", "--appendfsync", "always"]
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 10"""

_CI_BLOCK = """\
      redis:
        image: redis:7
        ports:
          - 6380:6379"""

_MICROSERVICES_INFRA = (
    '{"atomic_store": {"security": {"driver": "redis", "url": "redis://redis:6379/1"}}, '
    '"cache": {"default": {"url": "redis://redis:6379/0"}}, '
    '"broadcast": {"default": {"url": "redis://redis:6379/0"}}, '
    '"lease": {"beat": {"url": "redis://redis:6379/1"}}, '
    '"eventbus": {"default": {"url": "redis://redis:6379/1"}}}'
)

_MICROSERVICES_SERVICE = """\
  redis:
    <<: *service-runtime
    image: redis:7
    # Streams are the durable event log: persist them (AOF), and never evict
    # bus keys under memory pressure (writes fail loudly instead).
    command: ["redis-server", "--appendonly", "yes", "--maxmemory", "384mb", "--maxmemory-policy", "noeviction"]
    volumes:
      - redisdata:/data
    restart: unless-stopped
    deploy:
      resources:
        limits:
          cpus: "1.0"
          memory: 512m

  # Opt-in idempotency Redis (see the INFRA.atomic_store.idempotency note in the
  # shared env above). Same persistence as a security Redis — fsync every
  # write, never evict: a lost reservation re-runs a mutation.
  # idempotency-redis:
  #   <<: *service-runtime
  #   image: redis:7
  #   command: ["redis-server", "--appendonly", "yes", "--appendfsync", "always", "--maxmemory", "256mb", "--maxmemory-policy", "noeviction"]
  #   volumes:
  #     - idempotencyredisdata:/data
  #   restart: unless-stopped
  #   deploy:
  #     resources:
  #       limits:
  #         cpus: "1.0"
  #         memory: 384m"""

_MICROSERVICES_VOLUMES = """\
  redisdata:
  # idempotencyredisdata:   # with the opt-in idempotency-redis service"""


class RedisStackService:
    """:class:`~sandloom.driverkit.stack_service.StackService` of Redis."""

    name = "redis"
    test_name = "redis_test"
    label = "Redis"
    port_var = "SANDLOOM_DEV_REDIS_PORT"
    test_port_var = "SANDLOOM_DEV_TEST_REDIS_PORT"
    container_port = 6379
    local_port = _DEFAULT_PORT
    start_command = "brew services start redis"
    index_count = 1024
    #: A test run's cache base index and its persistent instances'.
    test_cache_index = 15
    test_broker_index = 14

    prod_compose = _PROD_BLOCK
    prod_volume = "redis_data"
    ci_service = _CI_BLOCK
    ci_port = 6380
    microservices_infra = _MICROSERVICES_INFRA
    microservices_service = _MICROSERVICES_SERVICE
    microservices_volumes = _MICROSERVICES_VOLUMES

    # ------------------------------------------------------------ dev tooling
    def server(self, host: str, port: int) -> RedisServer:
        return RedisServer(host, port)

    def parse(self, url: str) -> RedisServer:
        return RedisServer.from_url(url)

    def local_url(self, host: str) -> str:
        return f"redis://{_bracketed(host)}:{_DEFAULT_PORT}"

    def problem(self, server: KeyedServer) -> str | None:
        if not _tcp_open(server.host, server.port):
            return "connection refused"
        client = _connect(server)
        try:
            client.ping()
        except (redis.RedisError, OSError) as exc:
            return str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        finally:
            with contextlib.suppress(redis.RedisError, OSError):
                client.close()
        return None

    def clients(
        self, server: KeyedServer, indexes: Collection[int] | None = None
    ) -> int:
        client = _connect(server)
        try:
            own = int(client.execute_command("CLIENT", "ID"))
            wanted = None if indexes is None else set(indexes)
            count = 0
            for entry in client.client_list():
                if int(entry.get("id", -1)) == own:
                    continue
                if wanted is None or int(entry.get("db", 0)) in wanted:
                    count += 1
            return count
        finally:
            client.close()

    def empty_indexes(
        self, server: KeyedServer, candidates: Iterable[int], *, want: int = 2
    ) -> list[int]:
        """The first ``want`` of ``candidates`` (in order) that hold no key on
        ``server``, below its ``databases`` setting: a person's own Redis may
        keep data in any index, and ``reset`` / ``prune`` flush the ones given."""
        found: list[int] = []
        for index in candidates:
            client = _connect(server, index)
            try:
                size = int(client.dbsize())
            except redis.ResponseError:  # past the server's last database
                break
            finally:
                client.close()
            if size == 0:
                found.append(index)
                if len(found) == want:
                    break
        return found

    def flush(self, server: KeyedServer, indexes: Iterable[int]) -> None:
        for index in indexes:
            client = _connect(server, index)
            try:
                client.flushdb()
            finally:
                client.close()

    def errors(self) -> tuple[type[BaseException], ...]:
        return (redis.RedisError,)

    # ---------------------------------------------------------------- scaffold
    def dev_compose(self, port_hint: str, *, test: bool) -> str:
        name, var = (
            (self.test_name, self.test_port_var) if test else (self.name, self.port_var)
        )
        return _DEV_BLOCK.format(name=name, var=var, hint=port_hint)


#: What ``[project.entry-points."sandloom.stack"]`` registers.
STACK_SERVICE = RedisStackService()
