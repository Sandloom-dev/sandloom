"""Projection consistency tokens (spec §6.4 item 7): read-your-writes for an
eventually consistent read model, without ever turning lag into an error.

A write that feeds a projection returns ``SANDLOOM-Consistency-Token``: the
projection rows it will produce, as ``(projection, key digest, source group,
aggregate digest, version)`` entries, HMAC-SHA256-signed under
``PROJECTION_TOKEN_KEYS`` (``{kid: secret}``; the newest kid signs, every
listed kid verifies, so a key rotates by adding a kid). A later read
presenting the token waits up to ``PROJECTION_RYW_WAIT_MS`` for the
projection to reach those versions, then serves anyway with
``SANDLOOM-Projection-Stale: true`` (``on_stale="serve"``) or reads the entities
from their source (``on_stale="source"``).

An entry names its source group (the group's version column) and the
aggregate whose version it carries: the versions of independent groups are
never compared with one another, so a row whose other group is far ahead
(``title_version=1, like_version=100``) does not satisfy an entry of the
first group (``title_version >= 5``).

The token is advisory and never an error status:

* it expires after :data:`TOKEN_TTL_SECONDS` (600 s);
* it carries at most :data:`MAX_ENTRIES` (16) entries — a larger write gets
  no token at all (:func:`issue_token` returns ``None``);
* :func:`read_token` returns ``None`` for a missing, malformed, expired,
  unknown-kid, badly signed or other-tenant token (the read is then plainly
  eventual).

Pure kernel code: no I/O, no SQLAlchemy. The L1 projection layer
(``sandloom.sdk.projections``) issues tokens on writes and reads them in
``View(...)``; :func:`entries_satisfied` is the wait condition it polls.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, NamedTuple
from uuid import UUID

from sandloom.core.config.settings import settings

#: Request / response header carrying the token.
CONSISTENCY_HEADER = "SANDLOOM-Consistency-Token"
#: Response header of a read served before the projection caught up.
STALE_HEADER = "SANDLOOM-Projection-Stale"
#: Seconds a token stays valid.
TOKEN_TTL_SECONDS = 600
#: Entries one token may carry (a larger write returns no token).
MAX_ENTRIES = 16
#: Token format version (the first segment).
_VERSION = "v2"
#: Seconds between two reads while a token waits.
_POLL_SECONDS = 0.01


class TokenEntry(NamedTuple):
    """One row a write will produce: ``version`` of ``aggregate`` in the
    source ``group`` (its version column) of ``projection``'s row ``key``."""

    projection: str
    key: str  # row key digest
    group: str  # the source group's version column
    aggregate: str  # aggregate id digest
    version: int


#: ``(row key digest, group, aggregate digest)``: what one wait compares.
EntryKey = tuple[str, str, str]


@dataclass(frozen=True)
class ConsistencyToken:
    """A verified token (spec §2.3)."""

    kid: str
    issued_at: int  # epoch seconds
    tenant_id: UUID
    entries: tuple[TokenEntry, ...]

    @staticmethod
    def digest(key: Mapping[str, Any]) -> str:
        """The digest of a projection row key, as the entries carry it."""
        return key_digest(key)

    def versions(self, projection: str) -> dict[EntryKey, int]:
        """``(key digest, group, aggregate digest) -> version`` the token
        waits for in ``projection``: one version per source group and
        aggregate, never merged across groups."""
        out: dict[EntryKey, int] = {}
        for name, digest, group, aggregate, version in self.entries:
            if name == projection:
                slot = (digest, group, aggregate)
                out[slot] = max(version, out.get(slot, version))
        return out

    def keys(self, projection: str) -> set[str]:
        """The row key digests the token names in ``projection``."""
        return {e.key for e in self.entries if e.projection == projection}

    async def wait_for(
        self,
        projection: str,
        stored_version: Callable[[str, str], Awaitable[int | None]],
        *,
        wait_ms: int | None = None,
        refresh: Callable[[], Awaitable[None]] | None = None,
    ) -> bool:
        """Poll ``stored_version(digest, group)`` (the row's value of that
        group's version column) until ``projection`` reached every entry, at
        most ``wait_ms`` (default ``PROJECTION_RYW_WAIT_MS``); ``False`` when
        the budget ran out (serve stale, never an error). ``refresh`` runs
        before every round but the first (a caller polling cached rows
        rereads them there)."""
        wanted = self.versions(projection)
        if not wanted:
            return True
        budget = settings.projection_ryw_wait_ms if wait_ms is None else wait_ms
        deadline = time.monotonic() + max(0, budget) / 1000.0
        while True:
            for slot, version in list(wanted.items()):
                stored = await stored_version(slot[0], slot[1])
                if stored is not None and stored >= version:
                    del wanted[slot]
            if not wanted:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(_POLL_SECONDS, remaining))
            if refresh is not None:
                await refresh()


def aggregate_digest(aggregate: Any) -> str:
    """The digest of an aggregate id (UUID / str spell the same)."""
    return hashlib.sha256(str(aggregate).encode()).hexdigest()[:16]


def key_digest(key: Mapping[str, Any]) -> str:
    """A stable digest of a projection row key (column -> value): the same
    key always digests the same, whatever the mapping's order or the value's
    type spelling (UUID / str)."""
    canonical = json.dumps(
        {str(k): str(v) for k, v in key.items()},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(secret: str, body: str) -> str:
    return _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())


def _keys(keys: Mapping[str, str] | None) -> Mapping[str, str]:
    if keys is not None:
        return keys
    return dict(getattr(settings, "projection_token_keys", {}) or {})


def issue_token(
    entries: Iterable[tuple[str, Mapping[str, Any] | str, str, Any, int]],
    *,
    tenant_id: uuid.UUID,
    keys: Mapping[str, str] | None = None,
    now: Callable[[], float] = time.time,
) -> str | None:
    """The signed token of ``entries`` (``(projection, key or key digest,
    group, aggregate id, version)``; ``group`` is the source group's version
    column), or ``None``: no key configured, no entry, or more than
    :data:`MAX_ENTRIES` entries."""
    ring = _keys(keys)
    if not ring:
        return None
    normalised: list[tuple[str, str, str, str, int]] = []
    for projection, key, group, aggregate, version in entries:
        digest = key if isinstance(key, str) else key_digest(key)
        normalised.append(
            (
                str(projection),
                digest,
                str(group),
                aggregate_digest(aggregate),
                int(version),
            )
        )
        if len(normalised) > MAX_ENTRIES:
            return None
    if not normalised:
        return None
    kid = sorted(ring)[-1]  # the newest kid signs (kids sort by rotation)
    payload = {
        "k": kid,
        "t": int(now()),
        "n": str(tenant_id),
        "e": [list(e) for e in normalised],
    }
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return f"{_VERSION}.{body}.{_signature(ring[kid], body)}"


def read_token(
    header: str | None,
    *,
    tenant_id: uuid.UUID | None,
    keys: Mapping[str, str] | None = None,
    now: Callable[[], float] = time.time,
) -> ConsistencyToken | None:
    """The verified token, or ``None`` (never an error): missing, malformed,
    expired, unknown kid, bad signature, more than :data:`MAX_ENTRIES`
    entries, or a tenant other than the bound ``tenant_id``."""
    if not header or tenant_id is None:
        return None
    try:
        version, body, signature = header.strip().split(".")
        if version != _VERSION:
            return None
        payload = json.loads(_unb64(body))
        kid = str(payload["k"])
        secret = _keys(keys).get(kid)
        if secret is None:
            return None
        if not hmac.compare_digest(signature, _signature(secret, body)):
            return None
        issued = int(payload["t"])
        tenant = uuid.UUID(str(payload["n"]))
        entries = tuple(
            TokenEntry(str(p), str(d), str(g), str(a), int(v))
            for p, d, g, a, v in payload["e"]
        )
    except (ValueError, KeyError, TypeError, binascii.Error):
        return None
    age = now() - issued
    if age < -60 or age > TOKEN_TTL_SECONDS:  # 60 s of clock skew tolerated
        return None
    if tenant != tenant_id or not entries or len(entries) > MAX_ENTRIES:
        return None
    return ConsistencyToken(
        kid=kid, issued_at=issued, tenant_id=tenant, entries=entries
    )


def pending_entries(
    token: ConsistencyToken | None,
    projection: str,
    stored_version: Callable[[str, str], int | None],
) -> list[TokenEntry]:
    """The entries of ``projection`` the read model has not reached yet
    (``stored_version(digest, group)`` is the row's value of that group's
    version column, ``None`` for no row or no value): what a read waits
    for."""
    if token is None:
        return []
    missing: list[TokenEntry] = []
    for entry in token.entries:
        if entry.projection != projection:
            continue
        stored = stored_version(entry.key, entry.group)
        if stored is None or stored < entry.version:
            missing.append(entry)
    return missing


def entries_satisfied(
    token: ConsistencyToken | None,
    projection: str,
    stored_version: Callable[[str, str], int | None],
) -> bool:
    """Whether the read model reached every entry of ``projection``."""
    return not pending_entries(token, projection, stored_version)


__all__ = [
    "MAX_ENTRIES",
    "STALE_HEADER",
    "CONSISTENCY_HEADER",
    "TOKEN_TTL_SECONDS",
    "ConsistencyToken",
    "EntryKey",
    "TokenEntry",
    "aggregate_digest",
    "entries_satisfied",
    "issue_token",
    "key_digest",
    "pending_entries",
    "read_token",
]
