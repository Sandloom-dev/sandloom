"""New L2 effect primitives of ``sandloom.sdk.effects`` (ADR-0009 items 7-8).

The owner of a sensitive effect declares its risk once, at the source
(``@sensitive_effect("security", step_up=300)``); ``mark_reauthenticated()``
satisfies a step-up after a successful password check. Both are the
kernel's (H1, WS-A).
"""

from __future__ import annotations

from sandloom.core.di import mark_reauthenticated, sensitive_effect

__all__ = ["mark_reauthenticated", "sensitive_effect"]
