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

from functools import cache
from types import ModuleType
from typing import Any

from fastapi import Depends

import sandloom.core.di as _di
import sandloom.shared.authz as _authz


@cache
def _billing_stub() -> ModuleType | None:
    """``billing.entitlements.v1``'s stub as ``sandloom generate`` writes it
    for a consumer (the bench app has no ``shared/contracts``), or ``None`` on
    a kernel without the generator or the typed atoms (the baseline)."""
    try:
        from sandloom_billing.contracts import EntitlementsContract

        from sandloom.core.cli.generated import render_stub
    except ImportError:
        return None
    module = ModuleType("_bench_billing_entitlements_v1")
    try:
        source = render_stub("billing.entitlements.v1", EntitlementsContract)
        exec(compile(source, module.__name__, "exec"), module.__dict__)
    except Exception:  # an older generator: the baseline kernel's own atom
        return None
    return module if hasattr(module, "Subscribed") else None


def subscribed_atom() -> Any:
    """The ``Subscribed`` class: the generated atom, else the baseline kernel's
    (an older kernel still exports one; the current kernel ships none, so the
    name is looked up only on that path)."""
    stub = _billing_stub()
    return stub.Subscribed if stub is not None else getattr(_authz, "Subscribed")  # noqa: B009 - absent from the current kernel


def subscribed() -> Any:
    """The claims-only personal subscription atom."""
    atom = subscribed_atom()
    account = getattr(_authz, "Account", None)
    if account is not None:
        return atom(of=account.user())
    return atom()


def public(reason: str) -> list[Any]:
    """``[public(reason=...)]`` where the kernel denies by default, else ``[]``."""
    marker = getattr(_di, "public", None)
    return [marker(reason=reason)] if marker is not None else []


def legacy_marker(name: str, *args: Any, **kwargs: Any) -> list[Any]:
    """``[Depends(<retired marker>(...))]`` on a kernel that still has it."""
    factory = getattr(_di, name, None)
    return [Depends(factory(*args, **kwargs))] if factory is not None else []


__all__ = ["legacy_marker", "public", "subscribed", "subscribed_atom"]
