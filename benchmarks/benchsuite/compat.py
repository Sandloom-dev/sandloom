"""Kernel-version compatibility of the bench apps.

The §10.4 performance gate (Public API 1.0) runs the SAME bench code against
the baseline kernel (the integration branch's merge base) and the candidate.
The route declarations that changed between them are spelled here once:

* the subscription gate: ``Subscribed(of=Account.user())`` (candidate) /
  ``Subscribed()`` (baseline) — the same claims-only check;
* an unauthenticated route: ``public(reason=...)`` on the candidate (deny by
  default, ADR-0009); nothing on the baseline;
* the retired revocation markers (``strict_revocation()``,
  ``revocation_freshness(...)``): present only on the baseline (the candidate
  checks every route live).
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends

import sandloom.core.di as _di
import sandloom.shared.authz as _authz


def subscribed() -> Any:
    """The claims-only personal subscription atom."""
    account = getattr(_authz, "Account", None)
    if account is not None:
        return _authz.Subscribed(of=account.user())
    return _authz.Subscribed()


def public(reason: str) -> list[Any]:
    """``[public(reason=...)]`` where the kernel denies by default, else ``[]``."""
    marker = getattr(_di, "public", None)
    return [marker(reason=reason)] if marker is not None else []


def legacy_marker(name: str, *args: Any, **kwargs: Any) -> list[Any]:
    """``[Depends(<retired marker>(...))]`` on a kernel that still has it."""
    factory = getattr(_di, name, None)
    return [Depends(factory(*args, **kwargs))] if factory is not None else []


__all__ = ["legacy_marker", "public", "subscribed"]
