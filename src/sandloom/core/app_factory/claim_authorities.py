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
  map's issuer loaded without ``issues_tokens=True``); an
  ``issues_tokens=True`` module loaded with no map at all.
* **Issuer fields** (:data:`~sandloom.core.claims.ISSUER_FIELDS`: ``email``,
  ``email_verified``) are the map's issuer's: an entry naming another module
  (or naming one in a map without an issuer) is refused wherever the map is
  read; a map with an issuer that names none assigns them to the issuer. The
  issuer writes them at the mint through the same check
  (``collect_claims(issuer_claims=...)``, a critical step), so they need no
  claims contributor of their own.
* **The default map** (``default_claim_authorities``, a composition root's
  standard map, e.g. ``sandloom_standard.main``) keeps only the entries whose
  authority is part of this composition: loaded here, or bound through its
  ``<m>.claims.v1``; what it keeps is then checked like an explicit map. An
  explicit ``claim_authorities`` wins over it.
* **No map** (a lean root): nothing is assigned, so every protected claim is
  refused at the mint. A process that loads no ``issues_tokens=True`` module
  boots (it mints nothing); an issuing one refuses the boot (SANDLOOM-B035)
  instead of running an app whose every login is refused (503): the root
  that composes the issuer must assign its authorities.
"""

from __future__ import annotations

import difflib
import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from sandloom.core.app_factory.revocation_applier import ISSUER_CODE
from sandloom.core.claims import (
    ISSUER_ENTRY,
    ISSUER_FIELDS,
    NO_AUTHORITIES,
    Authority,
    ClaimAuthorities,
    LegacyClaimsTokens,
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
    """A ``claim_authorities`` entry that is not ``{str: str | Authority}``,
    or a ``legacy_tokens`` that is not an absolute UTC time."""


#: A ``claim_authorities`` value: an import path or entry-point name, or an
#: :class:`~sandloom.core.claims.Authority` (with a ``previous=`` one).
AuthorityEntry = str | Authority


def _resolve(entry: str) -> str:
    """The import path ``entry`` names: an entry-point name's path, else
    ``entry`` itself. A path is taken as written; whether a module of the
    same name is also installed is the loader's refusal (SANDLOOM-B020), and
    an authority is verified against what actually loaded."""
    if any(m.name == entry for m in installed_modules()):
        return resolve_module_names([entry])[0]
    return entry


def _entry_path(key: Any, value: Any) -> tuple[str, Authority | None]:
    """The import path of ``value`` and its ``previous=`` authority."""
    authority = value if isinstance(value, Authority) else None
    name = authority.name if authority is not None else value
    if not isinstance(key, str) or not key or not isinstance(name, str) or not name:
        raise ClaimAuthorityError(
            f"claim_authorities[{key!r}] = {value!r}: each claim key (and "
            f"{ISSUER_ENTRY!r}) maps to a module import path, an entry-point "
            "name or an Authority(...)"
        )
    prior = authority.previous if authority is not None else None
    if authority is not None and authority.module:
        return authority.module, prior
    try:
        return _resolve(name), prior
    except AmbiguousModuleError as exc:
        raise ClaimAuthorityError(
            f"claim_authorities[{key!r}] = {value!r} is ambiguous: {exc}"
        ) from exc


def _previous_entry(key: str, previous: Any) -> tuple[str, str]:
    """``(import path, short name)`` of a ``previous=`` authority: declared
    with its identity in full, never by short name (ADR-0014 decision 4)."""
    if (
        not isinstance(previous, Authority)
        or not previous.name
        or not previous.module
        or previous.previous is not None
    ):
        raise ClaimAuthorityError(
            f"claim_authorities[{key!r}]: previous={previous!r} names the "
            "authority by its identity in full, "
            "Authority('<name>', module='<import path>'), without a previous= "
            "of its own"
        )
    return previous.module, previous.name


def legacy_until(legacy_tokens: Any) -> float | None:
    """``legacy_tokens.until`` as a POSIX time (``None``: no compatibility
    mode); a :class:`ClaimAuthorityError` when it is not an absolute UTC
    time. Never derived, moved or extended here (G-RISK-14)."""
    if legacy_tokens is None:
        return None
    if not isinstance(legacy_tokens, LegacyClaimsTokens):
        raise ClaimAuthorityError(
            f"legacy_tokens={legacy_tokens!r}: pass "
            "LegacyClaimsTokens(until='<UTC time>')"
        )
    try:
        return legacy_tokens.until_timestamp()
    except (TypeError, ValueError) as exc:
        raise ClaimAuthorityError(str(exc)) from exc


def build_claim_authorities(
    mapping: Mapping[str, AuthorityEntry] | None, *, assigned: bool = True
) -> ClaimAuthorities:
    """Resolve a composition root's map (no check of the composition).

    Entry-point names become import paths, as in ``SANDLOOM_MODULES``; the
    ``issuer`` entry is kept apart from the claim keys. ``None`` is "no map"
    (:data:`~sandloom.core.claims.NO_AUTHORITIES`). The issuer fields are the
    issuer's: an entry naming another module for one is a
    :class:`ClaimAuthorityError`, and a map with an issuer that names none
    assigns them to it. An :class:`~sandloom.core.claims.Authority` value may
    declare one ``previous=`` authority a verifier also accepts for the key
    during a rollout (never for the issuer entry).
    """
    if mapping is None:
        return NO_AUTHORITIES
    authorities: dict[str, str] = {}
    previous: dict[str, str] = {}
    previous_owners: dict[str, str] = {}
    issuer: str | None = None
    for key, value in mapping.items():
        path, prior = _entry_path(key, value)
        if key == ISSUER_ENTRY:
            if prior is not None:
                raise ClaimAuthorityError(
                    f"claim_authorities[{ISSUER_ENTRY!r}]: the issuer takes no "
                    "previous= (only a claim key does)"
                )
            issuer = path
        else:
            authorities[key] = path
            if prior is not None:
                previous[key], previous_owners[key] = _previous_entry(key, prior)
    for key in sorted(ISSUER_FIELDS):
        if issuer is not None:
            authorities.setdefault(key, issuer)
        assigned_to = authorities.get(key)
        if assigned_to is not None and assigned_to != issuer:
            whose = f"({issuer!r})" if issuer else "(the map names none)"
            raise ClaimAuthorityError(
                f"claim_authorities[{key!r}] = {mapping[key]!r}: {key!r} is an "
                f"issuer field, written only by the map's {ISSUER_ENTRY!r} "
                f"{whose}; drop the entry or assign it to the issuer"
            )
    return ClaimAuthorities(
        authorities=authorities,
        owners={key: module_name(path) for key, path in authorities.items()},
        issuer=issuer,
        assigned=assigned,
        previous=previous,
        previous_owners=previous_owners,
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
    # The mint keeps only what is composed here; the verifier's map is the
    # root's whole map: in a split a verifier trusts an authority that runs
    # in another service, proven per key by the token's ``cpv`` digest and
    # provenance (decision 3). A key whose battery runs nowhere is never
    # stamped by an issuer, so it is never trusted.
    return ClaimAuthorities(
        authorities=kept,
        owners={key: authorities.owners[key] for key in kept},
        issuer=issuer,
        assigned=True,
        verified=authorities.verified,
        verified_owners=authorities.verified_owners,
        previous=authorities.previous,
        previous_owners=authorities.previous_owners,
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


def _no_map_problem(issuers: list[str]) -> str:
    """The refusal of an issuing process booted by a root with no map."""
    names = ", ".join(repr(path) for path in sorted(set(issuers)))
    return (
        f"no claim_authorities map is set, but {names} is loaded as the token "
        "issuer (issues_tokens): with no map no protected claim (roles, "
        "permissions, tenant_ids, email, ...) has an authority, so every "
        "token it mints would be refused (503); set "
        "SANDLOOM_COMPOSITION_ROOT=sandloom_standard.main (the standard root, "
        "which assigns the batteries' authorities) or your application's own "
        "root (<app>.asgi), or pass create_app(claim_authorities={...}) in the "
        "root that composes the issuer"
    )


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
    issuer_list = list(issuers)
    if not authorities.assigned:
        return [_no_map_problem(issuer_list)] if issuer_list else []
    problems: list[str] = []
    if issuer_list:
        # Only the issuing process mints, so only it needs every authority
        # loaded or bound; another process may not even have the module
        # installed (a split's own images).
        for key, path in sorted(authorities.authorities.items()):
            if key in ISSUER_FIELDS and path == authorities.issuer:
                # The issuer writes them in its own critical mint step; the
                # issuer itself is checked below.
                continue
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
    claim_authorities: Mapping[str, AuthorityEntry] | None,
    default_claim_authorities: Mapping[str, AuthorityEntry] | None,
    bindings: Mapping[str, Mapping[str, Any]],
    log: logging.Logger,
    legacy_tokens: LegacyClaimsTokens | None = None,
    lifetime_seconds: float = 0.0,
    leeway_seconds: float = 0.0,
) -> ClaimAuthorities:
    """Resolve, check and assign the composition's claim authorities.

    Raises :class:`ConfigurationError` (SANDLOOM-B035) naming every problem;
    on success the facade's mint check applies the result, and the trusted
    view (:mod:`sandloom.core.auth.claims_view`) reads it from this moment on
    (``effective_at``). ``legacy_tokens`` turns on the bounded compatibility
    mode of the initial ``cpv`` enablement (G-RISK-14); past its window end
    it is off by itself and a WARNING asks to remove it.
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
        until = legacy_until(legacy_tokens)
    except ClaimAuthorityError as exc:
        problems = [str(exc)]
        until = None
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
    if not authorities.assigned:
        log.info(
            "claim authorities: no claim_authorities map is set (no issuer is "
            "loaded here, so this process mints no token)"
        )
    else:
        log.info(
            "claim authorities: issuer=%s %s",
            authorities.issuer,
            dict(sorted(authorities.authorities.items())),
        )
    now = time.time()
    authorities = replace(authorities, legacy_until=until, effective_at=now)
    if until is not None:
        _log_legacy_mode(log, until + lifetime_seconds + leeway_seconds, now)
    core_facade._assign_claim_authorities(authorities)
    return authorities


def _log_legacy_mode(log: logging.Logger, window_end: float, now: float) -> None:
    """Startup WARNING of the compatibility mode (on, or ended)."""
    end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(window_end))
    if now >= window_end:
        log.warning(
            "claim authorities: the legacy_tokens window ended at %s: tokens "
            "without cpv give no rights; remove legacy_tokens from the "
            "composition root",
            end,
        )
    else:
        log.warning(
            "claim authorities: legacy_tokens compatibility mode is ON (accepted "
            "risk G-RISK-14): tokens without cpv are trusted per key until %s "
            "(%d s left)",
            end,
            int(window_end - now),
        )


__all__ = [
    "CODE",
    "AuthorityEntry",
    "ClaimAuthorityError",
    "build_claim_authorities",
    "claim_authority_problems",
    "filter_to_composition",
    "install_claim_authorities",
    "legacy_until",
]
