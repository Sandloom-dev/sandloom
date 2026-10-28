"""Shared bounds of the framework's dedupe / replay records (one source).

Every place that remembers "this operation already ran" and may replay its
outcome uses the same two bounds, so they cannot drift apart:

* :data:`REPLAY_RECORD_TTL_SECONDS` — how long a dedupe record lives by
  default: the HTTP ``idempotency_guard`` record, the durable command log
  (``CONTRACT_COMMAND_DEDUPE_TTL_SECONDS`` default), the cache-backed
  consumer inbox and the task-queue enqueue dedupe.
* :data:`REPLAY_BODY_MAX_BYTES` — the largest outcome kept for replay: the
  HTTP guard's captured response body and a command contract's encoded
  result. A larger outcome is stored as a non-replayable
  success marker (the effect never re-runs; only its body is not kept).

Intentionally NOT tied to these: ``EVENT_PAYLOAD_MAX_BYTES`` (also 256 KiB in
prod, but it bounds an event's size at publish — a different contract that
may be tuned independently) and the short ``pending`` reservation TTL of the
HTTP guard (minutes, self-healing an in-flight key).
"""

from __future__ import annotations

#: Default lifetime of a dedupe / replay record (24 h).
REPLAY_RECORD_TTL_SECONDS = 24 * 60 * 60
#: Largest outcome (response body / encoded result) kept for replay (256 KiB).
REPLAY_BODY_MAX_BYTES = 256 * 1024

__all__ = ["REPLAY_BODY_MAX_BYTES", "REPLAY_RECORD_TTL_SECONDS"]
