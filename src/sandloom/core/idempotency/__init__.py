"""Idempotency-Key handling for mutating endpoints.

A stateful Idempotency-Key layer with a proper lifecycle, not just a reserve:

* **pending** — a first request is in flight; a concurrent retry gets ``409``.
* **succeeded** — the stored response is REPLAYED on a retry (same status, body
  and content type), so a client retry gets the original result, not ``409``.
  This is best-effort: a response whose body exceeds the capture cap is stored
  as a non-replayable success marker (status only, ``Idempotent-Replay-Truncated``)
  — never re-executed, but its body is not preserved.
* **failed** — when the first attempt fails after reserving, the key is
  RELEASED (deleted), so a retry reserves afresh via an atomic SET NX. The key
  is never poisoned for its whole TTL, and two concurrent retries cannot both
  proceed (only one wins the SET NX).
* **fingerprint mismatch** — the same key reused for a *different* request
  (method/path/caller/body) is rejected with ``422`` (client misuse).

A FastAPI dependency cannot see the response, so the layer is split:

* :func:`idempotency_guard` — the dependency that makes the reserve / replay /
  retry / mismatch decision *before* the endpoint runs, and "arms" the request
  so its response is captured.
* :class:`IdempotencyCaptureMiddleware` — captures the armed request's response
  and writes the terminal ``succeeded`` / ``failed`` record.

Both reach the cache through the operation-based ``CacheProvider`` (never a raw
client). ``fail_closed`` controls behavior when the cache is unavailable.

That cache form is **replay-only** (ADR-0005): its terminal record is written
after ``COMMIT``, so a crash in between, or a ``COMMIT`` whose outcome the
request never learns (connection lost while committing), lets a retry re-run
an effect that committed. ``idempotency_guard(..., durable=True)`` is the
**durable** form (exactly-once effect on the request database's state): the
record is claimed in the REQUEST's own Unit of Work and completed with the
response right before ``COMMIT`` (:mod:`sandloom.core.sql_alchemy.request_log`), so
it commits iff the effect commits —

* same key + same fingerprint after a commit -> the stored response is
  replayed (``Idempotent-Replay: true``), the endpoint does not run;
* same key + another fingerprint -> ``422``;
* same key while the first request is still in flight -> ``409`` at once
  (a transaction-scoped advisory lock, never a wait on the first request);
* the request raises / its UoW rolls back -> no record; a retry runs afresh;
* the endpoint RETURNS a ``5xx`` response -> the record is dropped before
  ``COMMIT`` (``5xx`` is never replayed); any other returned status — a
  ``4xx`` included — is the committed outcome and is replayed;
* **ambiguous COMMIT**: the record is in the same transaction as the effect,
  so a retry either finds it (the commit happened -> replay) or claims afresh
  (it did not -> the effect never ran). Either way the effect applies once.

Effects OUTSIDE the request database (a PSP call, an email, another database)
stay at-least-once: the record cannot cover them (end-to-end argument) — key
them on the request's own idempotency key at the external system.

The package is split by responsibility: :mod:`.guard` (the dependency
factory), :mod:`.reservation` (reserve / keep alive / settle a cache key),
:mod:`.cache_store` (the cache operations), :mod:`.capture` (the response
capture middleware), :mod:`.durable` (the durable form), :mod:`.fingerprint`
and :mod:`.replay` (shared by both forms).
"""

from __future__ import annotations

from sandloom.core.idempotency.capture import IdempotencyCaptureMiddleware
from sandloom.core.idempotency.guard import idempotency_guard
from sandloom.core.idempotency.reservation import reserve_key

__all__ = [
    "idempotency_guard",
    "IdempotencyCaptureMiddleware",
    "reserve_key",
]
