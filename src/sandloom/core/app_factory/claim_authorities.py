"""The composition's claim authorities, resolved and checked at startup
(ADR-0014 decisions 1, 2 and 7).

``create_app(claim_authorities={key: module, ..., "issuer": module})`` is the
application's assignment, in its composition root (code, never a setting:
answer #9). A value is a module import path or a ``sandloom.modules``
entry-point name. :func:`install_claim_authorities` resolves it, refuses the
boot on an assignment it cannot honour (SANDLOOM-B035) and gives the facade
the result, which the mint check applies in the merge pass
(:meth:`sandloom.core.facade.CoreFacade.collect_claims`).

* **Verified, never named into trust.** A local authority is the module
  registered from the assigned import path: its short name and its loaded
  path both match. A remote authority is its ``<m>.claims.v1`` binding in
  ``SERVICE_BINDINGS``, which the configuration checks already require to be
  pinned to a ``service_id`` and authenticated with the pair MAC (ADR-0004).
* **Startup refusals**, each naming the key and the module, in the issuing
  process (only it mints; a split's other services may not even install the
  authority): an authority neither loaded nor bound (a typo of a loaded path
  is named as one; nothing is imported to tell), loaded under its short name
  from another path, loaded without a claims contributor, or non-critical
  for a key whose readers have
  no fallback (decision 7: only :data:`~sandloom.core.claims.FAIL_SAFE_KEYS`
  may have a non-critical authority, billing option (c)); an issuer mismatch
  (an ``issues_tokens=True`` module that is not the map's ``issuer``, or the
  map's issuer loaded without ``issues_tokens=True``).
* **The default map** (``default_claim_authorities``, a composition root's
  standard map, e.g. ``sandloom_standard.main``) keeps only the entries whose
  authority is part of this composition: loaded here, or bound through its
  ``<m>.claims.v1``; what it keeps is then checked like an explicit map. An
  explicit ``claim_authorities`` wins over it.
* **No map** (a lean root): nothing is assigned, so every protected claim is
  refused at the mint; an issuing process logs that at startup, and an issuer
  whose critical contributor writes a protected claim refuses every mint (503)
  rather than minting untrusted claims silently.
"""

from __future__ import annotations

import difflib
import logging
from collections.abc import Iterable, Mapping
from typing import Any

from sandloom.core.app_factory.revocation_applier import ISSUER_CODE
from sandloom.core.claims import (
    ISSUER_ENTRY,
    NO_AUTHORITIES,
    ClaimAuthorities,
)
from sandloom.core.config.common import INVALID_CONFIGURATION, ConfigurationError
from sandloom.core.descriptors.module_names import (
    AmbiguousModuleError,
    installed_modules,
    module_name,
    resolve_module_names,
)

#: The startup refusal code of every problem here (shared with the issuer's
#: session obligation: both are the token issuer's startup contract).
CODE = ISSUER_CODE
_CLAIMS_SUFFIX = ".claims.v1"


class ClaimAuthorityError(ValueError):
    """A ``claim_authorities`` entry that is not ``{str: str}``."""


def _resolve(entry: str) -> str:
    """The import path ``entry`` names: an entry-point name's path, else
    ``entry`` itself. A path is taken as written; whether a module of the
    same name is also installed is the loader's refusal (SANDLOOM-B020), and
    an authority is verified against what actually loaded."""
    if any(m.name == entry for m in installed_modules()):
        return resolve_module_names([entry])[0]
    return entry


def build_claim_authorities(
    mapping: Mapping[str, str] | None, *, assigned: bool = True
) -> ClaimAuthorities:
    """Resolve a composition root's map (no check of the composition).

    Entry-point names become import paths, as in ``SANDLOOM_MODULES``; the
    ``issuer`` entry is kept apart from the claim keys. ``None`` is "no map"
    (:data:`~sandloom.core.claims.NO_AUTHORITIES`).
    """
    if mapping is None:
        return NO_AUTHORITIES
    authorities: dict[str, str] = {}
    issuer: str | None = None
    for key, value in mapping.items():
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            or not value
        ):
            raise ClaimAuthorityError(
                f"claim_authorities[{key!r}] = {value!r}: each claim key (and "
                f"{ISSUER_ENTRY!r}) maps to a module import path or entry-point name"
            )
        try:
            path = _resolve(value)
        except AmbiguousModuleError as exc:
            raise ClaimAuthorityError(
                f"claim_authorities[{key!r}] = {value!r} is ambiguous: {exc}"
            ) from exc
        if key == ISSUER_ENTRY:
            issuer = path
        else:
            authorities[key] = path
    return ClaimAuthorities(
        authorities=authorities,
        owners={key: module_name(path) for key, path in authorities.items()},
        issuer=issuer,
        assigned=assigned,
    )


def _typo_hint(path: str, loaded: Mapping[str, str]) -> str:
    """``"; a typo of '<loaded path>'?"`` when ``path`` names no installed
    module and is close to a loaded one, else ``""``. No import: the check
    reads only what loaded and the installed entry points."""
    if any(m.path == path for m in installed_modules()):
        return ""
    close = difflib.get_close_matches(path, sorted(set(loaded.values())), n=1)
    return f"; a typo of {close[0]!r}?" if close else ""


def _in_composition(
    path: str, loaded: Mapping[str, str], bindings: Mapping[str, Any]
) -> bool:
    short = module_name(path)
    return loaded.get(short) == path or f"{short}{_CLAIMS_SUFFIX}" in bindings


def filter_to_composition(
    authorities: ClaimAuthorities,
    *,
    loaded: Mapping[str, str],
    bindings: Mapping[str, Any],
) -> ClaimAuthorities:
    """``authorities`` without the entries whose module is not composed here.

    An authority is part of the composition when it is loaded in this process
    or bound through its ``<m>.claims.v1``; the issuer when it is loaded. A
    key whose authority is left out stays unassigned (a definite deny).
    """
    kept = {
        key: path
        for key, path in authorities.authorities.items()
        if _in_composition(path, loaded, bindings)
    }
    issuer = authorities.issuer
    if issuer is not None and loaded.get(module_name(issuer)) != issuer:
        issuer = None
    return ClaimAuthorities(
        authorities=kept,
        owners={key: authorities.owners[key] for key in kept},
        issuer=issuer,
        assigned=True,
    )


def _local_authority_problem(
    key: str, path: str, critical: bool | None, authorities: ClaimAuthorities
) -> str | None:
    if critical is None:
        return (
            f"claim {key!r} is assigned to {path!r}, which is loaded in this "
            "issuing process but registers no claims contributor"
        )
    if not critical and authorities.critical_required(key):
        return (
            f"claim {key!r} is assigned to {path!r}, whose claims contributor is "
            "registered critical=False: only a claim whose every reader escalates "
            "or denies when it is absent may come from a non-critical authority "
            "(ADR-0014 decision 7); register the contributor critical=True"
        )
    return None


def _authority_problem(
    key: str,
    path: str,
    authorities: ClaimAuthorities,
    *,
    loaded: Mapping[str, str],
    contributors: Mapping[str, bool],
    bindings: Mapping[str, Mapping[str, Any]],
) -> str | None:
    """Why the issuing process cannot take ``key`` from ``path`` (else None)."""
    short = module_name(path)
    if loaded.get(short) == path:
        return _local_authority_problem(key, path, contributors.get(short), authorities)
    if short in loaded:
        return (
            f"claim {key!r} is assigned to {path!r}, but the module loaded here "
            f"under the name {short!r} is {loaded[short]!r}: an authority is the "
            "module from the assigned path, never a module of the same name"
        )
    binding = bindings.get(f"{short}{_CLAIMS_SUFFIX}")
    if binding is None:
        return (
            f"claim {key!r} is assigned to {path!r}, which is neither loaded in "
            f"this issuing process nor bound ({short}{_CLAIMS_SUFFIX} is not in "
            f"SERVICE_BINDINGS){_typo_hint(path, loaded)}"
        )
    if binding.get("critical", True) is False and authorities.critical_required(key):
        return (
            f"claim {key!r} is assigned to {path!r}, whose binding "
            f"{short}{_CLAIMS_SUFFIX} is critical: false: only a claim whose every "
            "reader escalates or denies when it is absent may come from a "
            "non-critical authority (ADR-0014 decision 7)"
        )
    return None


def _issuer_problems(
    authorities: ClaimAuthorities, loaded: Mapping[str, str], issuers: Iterable[str]
) -> list[str]:
    """The issuer cross-check of an assigned map (adversarial X2)."""
    expected = authorities.issuer
    problems = [
        f"module {path!r} declares issues_tokens=True but the claim_authorities "
        f"issuer is {expected!r}: name it as the {ISSUER_ENTRY!r} entry, or drop "
        "issues_tokens"
        for path in sorted(set(issuers))
        if path != expected
    ]
    if expected is not None:
        short = module_name(expected)
        if loaded.get(short) == expected and expected not in set(issuers):
            problems.append(
                f"the claim_authorities issuer {expected!r} is loaded here but does "
                "not declare issues_tokens=True"
            )
        elif short in loaded and loaded[short] != expected:
            problems.append(
                f"the claim_authorities issuer is {expected!r}, but the module "
                f"loaded here under the name {short!r} is {loaded[short]!r}"
            )
    return problems


def claim_authority_problems(
    authorities: ClaimAuthorities,
    *,
    loaded: Mapping[str, str],
    contributors: Mapping[str, bool],
    bindings: Mapping[str, Mapping[str, Any]],
    issuers: Iterable[str],
) -> list[str]:
    """Every reason the composition cannot honour ``authorities`` (empty: ok).

    ``loaded``: short name -> import path of every module loaded here;
    ``contributors``: short name -> critical, of every local claims owner;
    ``bindings``: ``SERVICE_BINDINGS``; ``issuers``: the import paths of the
    loaded ``issues_tokens=True`` modules (only an issuing process needs the
    authorities themselves).
    """
    if not authorities.assigned:
        return []
    issuer_list = list(issuers)
    problems: list[str] = []
    if issuer_list:
        # Only the issuing process mints, so only it needs every authority
        # loaded or bound; another process may not even have the module
        # installed (a split's own images).
        for key, path in sorted(authorities.authorities.items()):
            problem = _authority_problem(
                key,
                path,
                authorities,
                loaded=loaded,
                contributors=contributors,
                bindings=bindings,
            )
            if problem is not None:
                problems.append(problem)
    problems.extend(_issuer_problems(authorities, loaded, issuer_list))
    return problems


def install_claim_authorities(
    core_facade: Any,
    loaded_descriptors: Iterable[Any],
    *,
    claim_authorities: Mapping[str, str] | None,
    default_claim_authorities: Mapping[str, str] | None,
    bindings: Mapping[str, Mapping[str, Any]],
    log: logging.Logger,
) -> ClaimAuthorities:
    """Resolve, check and assign the composition's claim authorities.

    Raises :class:`ConfigurationError` (SANDLOOM-B035) naming every problem;
    on success the facade's mint check applies the result.
    """
    descriptors = list(loaded_descriptors)
    loaded = {module_name(str(d.name)): str(d.name) for d in descriptors}
    issuers = [str(d.name) for d in descriptors if getattr(d, "issues_tokens", False)]
    try:
        if claim_authorities is not None:
            authorities = build_claim_authorities(claim_authorities)
        elif default_claim_authorities is not None:
            authorities = filter_to_composition(
                build_claim_authorities(default_claim_authorities),
                loaded=loaded,
                bindings=bindings,
            )
        else:
            authorities = NO_AUTHORITIES
    except ClaimAuthorityError as exc:
        problems = [str(exc)]
    else:
        problems = claim_authority_problems(
            authorities,
            loaded=loaded,
            contributors={
                owner: critical
                for owner, critical in core_facade._local_claim_owners().items()
                if owner
            },
            bindings=bindings,
            issuers=issuers,
        )
    if problems:
        for problem in problems:
            log.error("claim authorities: %s", problem)
        raise ConfigurationError(
            INVALID_CONFIGURATION
            + f"{CODE}: "
            + "; ".join(problems)
            + f" (`sandloom explain {CODE}`)"
        )
    if not authorities.assigned and issuers:
        log.warning(
            "claim authorities: no claim_authorities map is set, so no protected "
            "claim (roles, permissions, tenant_ids, ...) has an authority and "
            "every contributor writing one is refused at the mint; pass "
            "create_app(claim_authorities=...) in the composition root (%s)",
            CODE,
        )
    else:
        log.info(
            "claim authorities: issuer=%s %s",
            authorities.issuer,
            dict(sorted(authorities.authorities.items())),
        )
    core_facade.assign_claim_authorities(authorities)
    return authorities


__all__ = [
    "CODE",
    "ClaimAuthorityError",
    "build_claim_authorities",
    "claim_authority_problems",
    "filter_to_composition",
    "install_claim_authorities",
]
