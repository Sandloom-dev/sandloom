"""The one error a not-yet-implemented Public API 1.0 name raises."""

from __future__ import annotations

from typing import NoReturn


def not_yet(name: str, section: str, lands: str) -> NoReturn:
    """Raise ``NotImplementedError`` for ``name`` (spec ``section``, ``lands``)."""
    raise NotImplementedError(
        f"sandloom.sdk: {name} is part of Public API 1.0 (spec {section}) and is "
        f"not implemented yet; it lands at {lands}. "
        "tests/fixtures/specs/public-api-1.0-spec.md"
    )
