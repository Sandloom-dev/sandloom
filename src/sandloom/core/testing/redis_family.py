"""The Redis harness family: the Redis halves of the test harness as one object.

Implements :class:`~sandloom.core.testing.families.HarnessFamily` with the Redis
code that lives beside it (:mod:`~sandloom.core.testing.redis_urls`,
:mod:`~sandloom.core.testing.redis_isolation`, the Redis half of
:mod:`~sandloom.core.testing.ownership` and
:mod:`~sandloom.core.testing.resources`). It is the kernel's one built-in family
until all of it moves into ``sandloom-redis``, which then registers it in the
entry-point group ``sandloom.testing``.

A process that cannot reach a Redis (the client is not installed, or no Redis
URL is set) makes every hook a no-op, and never imports
:mod:`~sandloom.core.testing.redis_isolation`.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Mapping, MutableMapping
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

from sandloom.core.testing import ownership, resources
from sandloom.core.testing.families import FLAT_VARS, INFRA_VAR
from sandloom.core.testing.redis_urls import (
    infra_redis_urls,
    is_redis_url,
    redis_client_installed,
    redis_isolation,
    redis_urls,
)


@runtime_checkable
class _RedisClientSource(Protocol):
    """A cache provider that hands out its asyncio Redis client."""

    async def redis_client(self) -> object: ...


class RedisFamily:
    """The Redis hooks of the harness (see :class:`HarnessFamily`)."""

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
        rewritten: dict[str, str] = redis_isolation().rewrite_redis(
            values, environ, worker_id, number
        )
        return rewritten

    def claim(self, values: Mapping[str, str]) -> None:
        for var in FLAT_VARS:
            url = values.get(var) or ""
            if url:
                ownership.claim_redis(url)
        for url in infra_redis_urls(values.get(INFRA_VAR)):
            ownership.claim_redis(url)

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
        environ.setdefault(redis_isolation().REDIS_STRIDE_ENV, "auto")
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
        isolation = redis_isolation()
        mapping = resources.allocate_worker_redis(
            urls,
            count,
            ownership.current_owner(),
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
            owner = os.environ.get(ownership.OWNER_ENV)
            room = resources.affordable_redis_workers(
                urls, owner or ownership.current_owner()
            )
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
        await ownership.flush_owned_redis(await provider.redis_client())
        return True

    @staticmethod
    def _auto() -> bool:
        """``SANDLOOM_TEST_REDIS_DB_STRIDE=auto`` on a process that can reach a
        Redis."""
        return redis_client_installed() and bool(redis_isolation().redis_auto())


FAMILY = RedisFamily()
