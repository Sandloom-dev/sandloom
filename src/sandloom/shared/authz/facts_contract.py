"""``<owner>.facts.v1`` — a module's access facts as a public contract.

Auto-registered by the kernel for every module that registers an access fact
(``core.register_access_fact``), exactly like ``<owner>.claims.v1`` for claims
contributors. It makes a fact usable from a process that does not load the
owner (a split deployment):

- ``describe()`` — every fact the owner registered, as wire-safe descriptors
  (:meth:`~sandloom.shared.authz.facts.AccessFact.to_wire`): claim key, the
  DECLARATIVE claim rule (computed with the owner's settings), freshness floor,
  default mode, deny category. A consumer builds its local view of the fact
  from it (:func:`~sandloom.shared.authz.facts.lookup_access_fact`), so claim mode
  interprets the token exactly like the owner does.
- ``evaluate(name, subject, request)`` — the owner's authoritative answer
  (its live resolver, else its projection reader), run in the owner's process
  where its own contracts and data live. Answers ``{"value": bool}``,
  ``{"denied": {reason, category, code}}`` (a resolver's ``FactDenied``) or
  ``{"unavailable": reason}`` (``FactUnavailable``).

Like every contract call, both run in the owner's own Unit of Work (read-only),
identically in-process and over internal RPC. The bound tenant travels in the
signed RPC caller context; ``request`` carries only path/query parameters and
the tenant selector header.
"""

from __future__ import annotations

from typing import Any

from sandloom.core.module_api import contract_method
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.authz.facts import (
    MODE_LIVE,
    MODE_PROJECTION,
    AccessFact,
    FactDenied,
    FactUnavailable,
    resolve_fact,
)
from sandloom.shared.subject import UserContext


class _WireRequest:
    """The request view an owner-side resolver sees (``path_params`` & co)."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.path_params: dict[str, Any] = dict(raw.get("path_params") or {})
        self.query_params: dict[str, Any] = dict(raw.get("query_params") or {})
        self.headers: dict[str, Any] = dict(raw.get("headers") or {})


class FactsContract:
    """Provider of ``<owner>.facts.v1`` for one fact-owning module."""

    def __init__(self, core: Any, owner: str) -> None:
        self._core = core
        self._owner = owner

    def _facts(self) -> dict[str, AccessFact]:
        listed = getattr(self._core, "list_access_facts", None)
        facts = listed() if callable(listed) else {}
        return {
            name: fact
            for name, fact in facts.items()
            if getattr(fact, "owner_module", None) == self._owner
        }

    @contract_method
    async def describe(self) -> list[dict[str, Any]]:
        """Wire-safe descriptors of every access fact this module owns."""
        return [fact.to_wire() for _name, fact in sorted(self._facts().items())]

    @contract_method
    async def evaluate(
        self,
        name: str,
        subject: dict[str, Any],
        request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The owner's authoritative answer for fact ``name`` and ``subject``."""
        fact = self._facts().get(name)
        if fact is None:
            return {"unavailable": f"access fact {name!r} is not owned here"}
        if fact.live_resolver is not None:
            mode = MODE_LIVE
        elif fact.projection_reader is not None:
            mode = MODE_PROJECTION
        else:
            return {"unavailable": f"access fact {name!r} has no live resolver"}
        ctx = AuthzContext(
            UserContext.model_validate(subject),
            request=_WireRequest(request) if request else None,  # type: ignore[arg-type]
            core=self._core,
            source="internal",
        )
        try:
            value = await resolve_fact(fact, ctx, mode=mode)
        except FactDenied as exc:
            return {
                "denied": {
                    "reason": exc.reason,
                    "category": exc.category,
                    "code": exc.code,
                }
            }
        except FactUnavailable as exc:
            return {"unavailable": str(exc)}
        return {"value": bool(value)}
