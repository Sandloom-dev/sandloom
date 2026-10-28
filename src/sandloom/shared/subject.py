"""The authenticated subject carried in the access token (``UserContext``)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

#: Top-level keys that are first-class ``UserContext`` fields (plus the ``sub``
#: alias). Every other token claim lands in the namespaced ``claims`` mapping.
_FIELD_KEYS = frozenset(
    {
        "user_id",
        "sub",
        "roles",
        "permissions",
        "claims_sources",
        "claims_sources_failed",
        "provenance_authoritative",
        "claims",
    }
)

#: Provenance values meaning "this contributor ran and contributed" when the
#: issuer stamps ``claims_sources`` as a mapping (``{"billing": "ok", ...}``).
_PROVENANCE_OK = frozenset({"ok", "contributed", "success", "true"})


def _normalize_sources(value: Any) -> set[str]:
    """Coerce the ``claims_sources`` claim into the set of TRUSTED modules.

    Accepts a list/set of module names, or a mapping ``{module: status}`` where
    only successful contributors (``True`` / ``"ok"`` / ``"contributed"`` …)
    count — a failed or skipped contributor (``"failed"``, ``False``) is NOT a
    trusted source. Anything else (absent, malformed) ⇒ empty ⇒ untrusted.
    """
    if value is None:
        return set()
    if isinstance(value, dict):
        trusted: set[str] = set()
        for module, status in value.items():
            if status is True or (
                isinstance(status, str) and status.lower() in _PROVENANCE_OK
            ):
                trusted.add(str(module))
        return trusted
    if isinstance(value, (list, tuple, set, frozenset)):
        return {str(v) for v in value if isinstance(v, str) and v}
    return set()


class UserContext(BaseModel):
    """The data context of an authenticated user.

    This schema is embedded into the JWT payload and used by decentralized
    guards. Standard JWTs carry the subject in ``sub``; both ``user_id`` and
    ``sub`` are accepted so a token minted by identity validates without
    remapping.

    Only module-agnostic identity fields are first-class. Claims contributed by
    other modules (billing's ``subscription_status`` / ``plan_code`` /
    ``features`` / ``limits``, tenancy's ``tenant_ids`` …) live in the
    namespaced :attr:`claims` mapping and are read with :meth:`claim`; any
    other keyword (``UserContext(subscription_status=...)``) is routed into
    ``claims`` the same way.
    """

    model_config = ConfigDict(populate_by_name=True)

    user_id: UUID = Field(validation_alias=AliasChoices("user_id", "sub"))

    # From 'identity' module
    roles: set[str] = Field(default_factory=set)
    permissions: set[str] = Field(default_factory=set)

    #: Every other claim of the token, keyed by its claim name (contributed by
    #: per-module claims contributors at mint time).
    claims: dict[str, Any] = Field(default_factory=dict)

    # Provenance: which claim-contributing modules ran at mint time (derived
    # from each contributor's package). A claim-mode access fact is only
    # trustworthy when its owning module appears here — otherwise the token was
    # minted by a process that did not load that module (or the contributor
    # failed), and the fact must be resolved live / from a projection instead.
    # Absent / empty ⇒ nothing is trusted. See sandloom.shared.authz.facts.
    claims_sources: set[str] = Field(default_factory=set)

    #: Claim-contributing modules that FAILED at mint time (their claims are
    #: absent, not "false"). Stamped by every current issuer together with
    #: ``claims_sources``.
    claims_sources_failed: set[str] = Field(default_factory=set)

    #: True when the token carried the ``claims_sources_failed`` key — i.e. it
    #: was minted by an issuer that stamps authoritative provenance, so an
    #: EMPTY ``claims_sources`` really means "no module contributed". False for
    #: a legacy token (provenance keys absent).
    provenance_authoritative: bool = False

    @model_validator(mode="before")
    @classmethod
    def _collect_claims(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        known = {k: v for k, v in data.items() if k in _FIELD_KEYS}
        extra = {k: v for k, v in data.items() if k not in _FIELD_KEYS}
        claims = dict(known.get("claims") or {})
        claims.update(extra)
        known["claims"] = claims
        known["claims_sources"] = _normalize_sources(known.get("claims_sources"))
        failed = known.get("claims_sources_failed")
        known["claims_sources_failed"] = (
            {str(v) for v in failed if isinstance(v, str) and v}
            if isinstance(failed, (list, tuple, set, frozenset))
            else set()
        )
        known.setdefault("provenance_authoritative", "claims_sources_failed" in data)
        return known

    def claim(self, key: str, default: Any = None) -> Any:
        """Return the token claim ``key`` (any contributing module), or default."""
        return self.claims.get(key, default)
