"""The level a one-shot ``sandloom`` command asks the log for.

A leaf: it imports nothing (not :mod:`sandloom.core.config`), so the static
commands (``sandloom new`` / ``add``) can set it without building the settings
object. :func:`sandloom.core.observability.logging.init_logging` reads it.

It is a default, never an override: it applies only while ``LOG_LEVEL`` is set
nowhere (not exported, not in ``.env``), and it writes no environment variable,
so ``sandloom config dump`` keeps reporting where the configured level comes
from.
"""

from __future__ import annotations


class _Requested:
    level: str | None = None


def request_default(level: str | None) -> None:
    """Ask for ``level`` (``"WARNING"``) while ``LOG_LEVEL`` is not set;
    ``None`` withdraws the request."""
    _Requested.level = level


def requested_default() -> str | None:
    """The level a command asked for, or ``None``."""
    return _Requested.level


__all__ = ["request_default", "requested_default"]
