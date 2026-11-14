"""What an ``atomic_store`` driver shares with every other one.

The argument checks every driver makes first (so one key, TTL or value is
refused the same way whatever the backend), the value domain of
``set_subject_state`` with its exact ranking, and the performance hints a
driver declares. A driver package imports them from here, never from
``sandloom.core`` (ADR-0016); the kernel's own drivers (``redis``, ``memory``)
import them from here too. The names here are the port's own.

- :func:`check_key`, :func:`check_value`, :func:`check_ttl`: ``InvalidArgument``
  (``TooLarge`` for an oversized value; a TTL over ``MAX_TTL_SECONDS`` is
  ``InvalidArgument``) before the backend is asked.
- :func:`check_subject_state`, :func:`subject_state_rank`,
  :func:`parse_subject_state`: ``v1:after:<decimal>`` / ``v1:blocked:<decimal>``;
  every ``blocked`` ranks above every ``after``, within a kind the instant
  compares as an exact :class:`~decimal.Decimal`, never a float or text; a
  value over ``MAX_SUBJECT_STATE_CHARS`` is ``TooLarge``.
"""

from __future__ import annotations

from sandloom.core.infra.ports.atomic_store import (
    ATOMIC_STORE_PERFORMANCE_HINTS,
    DEFAULT_MAX_VALUE_BYTES,
    MAX_KEY_BYTES,
    MAX_SUBJECT_STATE_CHARS,
    MAX_TTL_SECONDS,
    SUBJECT_STATE_RANKS,
    check_key,
    check_subject_state,
    check_ttl,
    check_value,
    parse_subject_state,
    subject_state_rank,
)

__all__ = [
    "ATOMIC_STORE_PERFORMANCE_HINTS",
    "DEFAULT_MAX_VALUE_BYTES",
    "MAX_KEY_BYTES",
    "MAX_SUBJECT_STATE_CHARS",
    "MAX_TTL_SECONDS",
    "SUBJECT_STATE_RANKS",
    "check_key",
    "check_subject_state",
    "check_ttl",
    "check_value",
    "parse_subject_state",
    "subject_state_rank",
]
