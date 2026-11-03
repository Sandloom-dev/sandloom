"""The administrator's password rules, as a prompt states them.

``sandloom admin create`` says the minimum length in its prompt ("at least
N characters"): identity's own (``sandloom_identity.password_policy``) when
importable, else a length rule alone. The check itself stays identity's
(``password_problem`` of ``identity.security.v1``).

The kernel names no module: identity's policy is looked up by its import
path at run time, and its absence is an ordinary case (identity may run as
its own service).
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType

#: The policy module of the standard identity module (an import path).
IDENTITY_POLICY = "sandloom_identity.password_policy"
#: The bounds used when identity's policy cannot be imported.
FALLBACK_MIN_LENGTH = 8
FALLBACK_MAX_LENGTH = 100


@dataclass(frozen=True)
class Rules:
    min_length: int
    max_length: int
    #: identity's ``password_policy_violation(password, email)``, or ``None``.
    check: Callable[[str, str | None], str | None] | None = None

    @property
    def identity(self) -> bool:
        """Whether identity's own policy decides (not only the length)."""
        return self.check is not None


def _identity_policy() -> ModuleType | None:
    try:
        return importlib.import_module(IDENTITY_POLICY)
    except ImportError:
        return None


def rules() -> Rules:
    """identity's rules when importable, else the length rule alone."""
    policy = _identity_policy()
    if policy is None:
        return Rules(FALLBACK_MIN_LENGTH, FALLBACK_MAX_LENGTH)
    return Rules(
        int(getattr(policy, "MIN_PASSWORD_LENGTH", FALLBACK_MIN_LENGTH)),
        int(getattr(policy, "MAX_PASSWORD_LENGTH", FALLBACK_MAX_LENGTH)),
        getattr(policy, "password_policy_violation", None),
    )


def problem(password: str, email: str | None = None) -> str | None:
    """Why ``password`` would be refused (one line), or ``None``."""
    found = rules()
    if len(password) < found.min_length:
        return f"Password must be at least {found.min_length} characters"
    if len(password) > found.max_length:
        return f"Password must be at most {found.max_length} characters"
    return found.check(password, email) if found.check is not None else None


__all__ = ["Rules", "problem", "rules"]
