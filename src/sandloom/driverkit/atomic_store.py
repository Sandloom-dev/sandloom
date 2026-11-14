"""What an ``atomic_store`` driver shares with every other one.

The argument checks every driver makes first (so one key, TTL or value is
refused the same way whatever the backend), the value domain of
``set_subject_state`` with its exact ranking, and the performance hints a
driver declares. Import them from here, never from ``sandloom.core``: a driver
imports only ``sandloom.driverkit`` (ADR-0016).

- :func:`check_key`, :func:`check_value`, :func:`check_ttl`: ``InvalidArgument``
  (``TooLarge`` for an oversized value) before the backend is asked.
- :func:`check_subject_state`, :func:`subject_state_rank`,
  :func:`parse_subject_state`: ``v1:after:<decimal>`` / ``v1:blocked:<decimal>``;
  every ``blocked`` ranks above every ``after``, within a kind the instant
  compares as an exact :class:`~decimal.Decimal`, never a float or text.
"""

from __future__ import annotations

from sandloom.core.infra.ports.atomic_store import (
    ATOMIC_STORE_PERFORMANCE_HINTS,
    DEFAULT_MAX_VALUE_BYTES,
    MAX_KEY_BYTES,
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
    "SUBJECT_STATE_RANKS",
    "check_key",
    "check_subject_state",
    "check_ttl",
    "check_value",
    "parse_subject_state",
    "subject_state_rank",
]
