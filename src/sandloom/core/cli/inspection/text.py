"""Human text formatting; structured inspection evidence remains unchanged."""

from __future__ import annotations

import re

_FRESHNESS = {
    "verified": "verified",
    "source_changed": "source changed since last generate",
    "missing_receipt": "not verified on this checkout",
    "dependency_changed": "declarations or dependencies changed since last generate",
    "partial": "verification incomplete for this scope",
}


def freshness_label(state: str) -> str:
    return _FRESHNESS.get(state, state)


def docstring_text(value: str) -> str:
    return re.sub(r"``([^`\n]+)``", r"`\1`", value)


def count_label(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")
