"""The Redis harness family: the Redis halves of the test harness as one object.

Implements :class:`~sandloom.driverkit.testing_family.HarnessFamily` with the
Redis code beside it (:mod:`sandloom_redis.testing.urls`,
:mod:`~sandloom_redis.testing.isolation`, :mod:`~sandloom_redis.testing.ownership`
and :mod:`~sandloom_redis.testing.resources`) and is registered in the
entry-point group ``sandloom.testing`` by this package's ``pyproject.toml``.

A process that cannot reach a Redis (the client is not installed, or no Redis
URL is set) makes every hook a no-op, and never imports
:mod:`~sandloom_redis.testing.isolation`.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Mapping, MutableMapping
from typing import Protocol, runtime_checkable
from urllib.parse import parse_qs, urlunsplit

from sandloom.driverkit.shared_endpoints import redis_endpoint
from sandloom.driverkit.testing_family import (
    INFRA_VAR,
    OWNER_ENV,
    current_owner,
)
from sandloom.driverkit.url_split import split_url
from sandloom_redis.testing import resources
from sandloom_redis.testing.ownership import claim_redis, flush_owned_redis
from sandloom_redis.testing.urls import (
    infra_redis_urls,
    is_redis_url,
    redis_client_installed,
    redis_urls,
    refuse_unplain_database,
)


@runtime_checkable
class _RedisClientSource(Protocol):
    """A cache provider that hands out its asyncio Redis client."""

    async def redis_client(self) -> object: ...


def _identity(url: str) -> tuple[object, ...] | None:
    """What makes two Redis URLs the same database, or ``None`` for a URL that
    names none (``redis_endpoint`` reads no endpoint: a malformed port, an
    unencoded ``/`` in the password, an unbracketed IPv6 host, a scheme that is
    not Redis's).

    The server and database are ``redis_endpoint``'s, the one reader of them
    (host lower-cased, port 6379 when absent, the database as redis-py reads
    it). A host spelled with a trailing dot stays a different one: it is an
    absolute name, the other may be completed by the resolver. The scheme, the
    credentials and the query options other than ``db`` are added. The ``db``
    option is left out of those options only so that a URL still carrying one
    compares on the database it names; every entry point that claims or moves a
    URL refuses it.
    """
    endpoint = redis_endpoint(url)
    if endpoint is None:
        return None
    parts = split_url(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    query.pop("db", None)
    return (
        parts.scheme,
        parts.netloc.rpartition("@")[0],
        endpoint,
        sorted((k, tuple(v)) for k, v in query.items()),
    )


class RedisFamily:
    """The Redis hooks of the harness (see ``HarnessFamily``)."""

    name = "redis"

    def rewrite_worker(
        self,
        values: Mapping[str, str | None],
        environ: Mapping[str, str],
        worker_id: str,
        number: int,
    ) -> dict[str, str]:
        if not (INFRA_VAR in values and redis_client_installed()):
            return {}
        from sandloom_redis.testing import isolation

        return isolation.rewrite_redis(values, environ, worker_id, number)

    def claim(self, values: Mapping[str, str]) -> None:
        for url in infra_redis_urls(values.get(INFRA_VAR)):
            claim_redis(url)

    def slot_bases(
        self,
        urls: Mapping[str, str],
        owner: str,
        environ: MutableMapping[str, str],
    ) -> dict[str, str]:
        """New base indexes for this owner, one per distinct base index."""
        plain = [u for u in urls.values() if is_redis_url(u)]
        if not plain or not redis_client_installed():
            return {}  # no Redis URL, or no client: this run reaches no Redis
        for url in plain:
            refuse_unplain_database(url)
        from sandloom_redis.testing import isolation

        environ.setdefault(isolation.REDIS_STRIDE_ENV, "auto")
        redis_error = resources.redis_errors()
        try:
            mapping = resources.allocate_worker_redis(plain, 1, owner)
        except redis_error as exc:
            raise RuntimeError(str(exc)) from exc
        out: dict[str, str] = {}
        for var, url in urls.items():
            parts = split_url(url)
            raw = parts.path.lstrip("/") or "0"
            if not raw.isdigit() or int(raw) not in mapping:
                continue
            out[var] = urlunsplit(parts._replace(path=f"/{mapping[int(raw)][0]}"))
        return out

    def release_slot_bases(self, urls: Iterable[str], owner: str) -> None:
        for url in urls:
            if is_redis_url(url):
                resources.release_redis(url, owner)

    def allocate_workers(self, values: Mapping[str, str], count: int) -> None:
        if not self._auto():
            return
        urls = redis_urls(values)
        if not urls:
            return
        from sandloom_redis.testing import isolation

        mapping = resources.allocate_worker_redis(
            urls,
            count,
            current_owner(),
            spares=resources.REDIS_SPARES_PER_BASE,  # replacements of crashed workers
        )
        os.environ[isolation.REDIS_MAP_ENV] = isolation.format_redis_map(mapping)

    def cap_workers(self, values: Mapping[str, str], count: int) -> int:
        if not self._auto():
            return count
        urls = redis_urls(values)
        if not urls:
            return count
        try:
            owner = os.environ.get(OWNER_ENV)
            room = resources.affordable_redis_workers(urls, owner or current_owner())
        except resources.redis_errors():  # reported again at session start
            return count
        if room >= count:
            return count
        fallback = room if room > 1 else 0
        sys.stderr.write(
            f"sandloom: only {room} xdist workers' worth of Redis database "
            f"indexes are free (wanted {count}); running "
            + (f"{fallback} workers" if fallback else "serially")
            + ". Free stale ones: `uv run python "
            "tools/checks/test_resources.py "
            "prune --apply`.\n"
        )
        return fallback

    async def flush_owned(self, provider: object) -> bool:
        if not isinstance(provider, _RedisClientSource):
            return False
        await flush_owned_redis(await provider.redis_client())
        return True

    def owns_url(self, url: str) -> bool:
        return is_redis_url(url)

    def same_database(self, stated: str, base: str) -> bool:
        """Same scheme, credentials, host, port and database index (a URL without
        a path is database 0; ``/00`` and ``/0`` are the same index, as
        ``slot_bases`` reads them), and the same other query options. The host
        is compared as the connection uses it: its case and the implicit port
        6379 do not tell two URLs apart, a trailing dot does. A URL that names
        no endpoint (a malformed port, an unbracketed IPv6 host, a password with
        an unencoded ``/``) is the same as none: ``False``, whatever the other
        is."""
        a, b = _identity(stated), _identity(base)
        return a is not None and a == b

    def parallel_env(self) -> dict[str, str]:
        if not redis_client_installed():
            return {}  # this run reaches no Redis: nothing to allocate
        from sandloom_redis.testing import isolation

        return {isolation.REDIS_STRIDE_ENV: isolation.REDIS_STRIDE_AUTO}

    @staticmethod
    def _auto() -> bool:
        """``SANDLOOM_TEST_REDIS_DB_STRIDE=auto`` on a process that can reach a
        Redis."""
        if not redis_client_installed():
            return False
        from sandloom_redis.testing import isolation

        return isolation.redis_auto()


FAMILY = RedisFamily()
