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
from urllib.parse import urlsplit, urlunsplit

from sandloom_redis.testing import resources
from sandloom_redis.testing.ownership import claim_redis, flush_owned_redis
from sandloom_redis.testing.urls import (
    infra_redis_urls,
    is_redis_url,
    redis_client_installed,
    redis_urls,
)

from sandloom.driverkit.testing_family import (
    FLAT_VARS,
    INFRA_VAR,
    OWNER_ENV,
    current_owner,
)


@runtime_checkable
class _RedisClientSource(Protocol):
    """A cache provider that hands out its asyncio Redis client."""

    async def redis_client(self) -> object: ...


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
        has_redis_setting = INFRA_VAR in values or any(
            is_redis_url(values.get(var) or "") for var in FLAT_VARS
        )
        if not (has_redis_setting and redis_client_installed()):
            return {}
        from sandloom_redis.testing import isolation

        return isolation.rewrite_redis(values, environ, worker_id, number)

    def claim(self, values: Mapping[str, str]) -> None:
        for var in FLAT_VARS:
            url = values.get(var) or ""
            if url:
                claim_redis(url)
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
        from sandloom_redis.testing import isolation

        environ.setdefault(isolation.REDIS_STRIDE_ENV, "auto")
        redis_error = resources.redis_errors()
        try:
            mapping = resources.allocate_worker_redis(plain, 1, owner)
        except redis_error as exc:
            raise RuntimeError(str(exc)) from exc
        out: dict[str, str] = {}
        for var, url in urls.items():
            parts = urlsplit(url)
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

    @staticmethod
    def _auto() -> bool:
        """``SANDLOOM_TEST_REDIS_DB_STRIDE=auto`` on a process that can reach a
        Redis."""
        if not redis_client_installed():
            return False
        from sandloom_redis.testing import isolation

        return isolation.redis_auto()


FAMILY = RedisFamily()
