"""identity's password policy: the length bounds and the violation check.

Moved here from the kernel's CLI tests (B-I1, ADR-0012 decision 6): the kernel
no longer imports this module, and ``sandloom admin create`` reaches the policy
only through ``password_problem`` of ``identity.security.v1``.
"""

from __future__ import annotations

from sandloom_identity import password_policy


def test_the_identity_policy_counts_the_length() -> None:
    """A short password is refused, a long mixed one is accepted."""
    assert password_policy.MIN_PASSWORD_LENGTH == 8
    assert password_policy.password_policy_violation("short") is not None
    assert password_policy.password_policy_violation("Correct-Horse-Battery-9") is None
