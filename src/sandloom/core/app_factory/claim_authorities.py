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
  for a key not declared fail-safe (decision 7: only a key whose every reader
  escalates or answers indeterminate when it is absent may have a
  non-critical authority, billing option (c); its local authority declares
  it with ``add_claims_contributor(..., fail_safe=...)``, the root with
  ``Authority(..., fail_safe=True)``, the one declaration a remote authority
  has here; the kernel names no such key); an issuer mismatch
  (an ``issues_tokens=True`` module that is not the map's ``issuer``, or the
  map's issuer loaded without ``issues_tokens=True``); an
  ``issues_tokens=True`` module loaded with no map at all.
* **Issuer fields** (:data:`~sandloom.core.claims.ISSUER_FIELDS`: ``email``,
  ``email_verified``) are the map's issuer's: an entry naming another module
  (or naming one in a map without an issuer) is refused wherever the map is
  read; a map with an issuer that names none assigns them to the issuer. The
  issuer's own claims contributor writes them at the mint through the same
  check (no caller hands the mint a value). They have no other writer and no
  fallback (decision 7), so the issuing process refuses an issuer that
  registers no claims contributor or registers it ``critical=False``.
* **The default map** (``default_claim_authorities``, a composition root's
  standard map, e.g. ``sandloom_standard.main``) keeps only the entries whose
  authority is part of this composition: loaded here, or bound through its
  ``<m>.claims.v1``; what it keeps is then checked like an explicit map. An
  explicit ``claim_authorities`` wins over it. A loaded issuer that is not
  the default map's issuer, while that one was left out, is refused naming
  the left-out issuer and the root to write.
* **No map** (a lean root): nothing is assigned, so every protected claim is
  refused at the mint. A process that loads no ``issues_tokens=True`` module
  boots (it mints nothing); an issuing one refuses the boot (SANDLOOM-B035)
  instead of running an app whose every login is refused (503): the root
  that composes the issuer must assign its authorities. The refusal names
  the booted root, and an installed root whose map names that issuer when
  one registered it (:data:`ROOTS_GROUP`).
"""

from __future__ import annotations

import difflib
import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import replace
from importlib import metadata
from typing import Any

import sandloom.shared.authz.contract_check as _contract_check
from sandloom.core.app_factory.revocation_applier import ISSUER_CODE
from sandloom.core.claims import (
    ISSUER_ENTRY,
    ISSUER_FIELDS,
    NEVER_FAIL_SAFE,
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
#: The entry-point group under which a distribution's composition root
#: registers its claim-authority map (name: the root module, object: the
#: map), so a refusal can say which installed root composes an issuer
#: without the kernel naming one. Read only on a refusal.
ROOTS_GROUP = "sandloom.composition_roots"
#: The kernel's lean root, booted when SANDLOOM_COMPOSITION_ROOT is unset.
LEAN_ROOT = "sandloom.main"


class ClaimAuthorityError(ValueError):
    """A ``claim_authorities`` entry that is not ``{str: str | Authority}``,
    or a ``legacy_tokens`` that is not an absolute UTC time."""


class UnprotectedClaimReader(RuntimeError):
    """SANDLOOM-B035: a generated claim atom imported after ``create_app``
    reads a claim key the composition's policy, fixed at boot, does not
    protect (a critical startup task refuses the start)."""

    sandloom_code = CODE


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
    with its identity in full, never by short name (ADR-0014 decision 4).

    The verifier reads the previous authority's provenance under ``name``,
    so ``name`` must be the name ``module`` is loaded under: when the path
    is installed here (an entry point), its entry-point name. A path no
    longer installed (the old module left the image after the rollout) keeps
    the declared name: its entry-point name cannot be read back, and the
    last import segment is not it (``sandloom_identity`` is ``identity``).
    """
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
    if any(m.path == previous.module for m in installed_modules()):
        loaded_as = module_name(previous.module)
        if loaded_as != previous.name:
            raise ClaimAuthorityError(
                f"claim_authorities[{key!r}]: previous={previous!r} names "
                f"{previous.module!r} {previous.name!r}, but it is installed as "
                f"{loaded_as!r}: write Authority({loaded_as!r}, "
                f"module={previous.module!r})"
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


def _declared_fail_safe(key: str, value: Any) -> bool:
    """Whether the root declares ``key`` fail-safe (``Authority(...,
    fail_safe=True)``); a :class:`ClaimAuthorityError` for a key that never
    is (decision 7) or the issuer entry."""
    if not isinstance(value, Authority) or not value.fail_safe:
        return False
    if key == ISSUER_ENTRY or key in NEVER_FAIL_SAFE:
        raise ClaimAuthorityError(
            f"claim_authorities[{key!r}]: fail_safe=True refused: roles, "
            "permissions, tenant_ids, the issuer's fields and the issuer entry "
            "are never fail-safe (ADR-0014 decision 7)"
        )
    return True


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
    fail_safe: set[str] = set()
    issuer: str | None = None
    for key, value in mapping.items():
        path, prior = _entry_path(key, value)
        if _declared_fail_safe(key, value):
            fail_safe.add(key)
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
        fail_safe=frozenset(fail_safe),
    )


def declared_policy(
    authorities: ClaimAuthorities,
    *,
    loaded: Mapping[str, str],
    declared: Mapping[str, frozenset[str]],
    readers: frozenset[str] = frozenset(),
) -> ClaimAuthorities:
    """``authorities`` with what this composition's modules declare.

    ``declared``: local claims-contributor owner (short name) -> the keys it
    declares fail-safe; ``readers``: the claim keys the generated claim
    atoms imported here read. A key is fail-safe (its authority may be
    non-critical, ADR-0014 decision 7) when the root marks it
    ``Authority(..., fail_safe=True)`` or its assigned authority is loaded
    here from the assigned path and declares it; every declared key and
    every key a reader reads is protected (:attr:`ClaimAuthorities.declared`).
    Boot only: the declarations are in memory, nothing is imported or read.
    """
    fail_safe = set(authorities.fail_safe)
    for key, path in authorities.authorities.items():
        short = module_name(path)
        if loaded.get(short) == path and key in declared.get(short, frozenset()):
            fail_safe.add(key)
    keys: set[str] = set(readers)
    for owned in declared.values():
        keys |= owned
    return replace(
        authorities,
        fail_safe=frozenset(fail_safe),
        declared=authorities.declared | frozenset(keys),
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
        fail_safe=authorities.fail_safe & frozenset(kept),
        declared=authorities.declared,
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
            "or answers indeterminate when it is absent may come from a "
            "non-critical authority (ADR-0014 decision 7); register the "
            "contributor critical=True, or declare the key with "
            "add_claims_contributor(..., fail_safe=[...])"
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
            "reader escalates or answers indeterminate when it is absent may "
            "come from a non-critical authority (ADR-0014 decision 7); bind it "
            "critical, or declare the key fail-safe in the composition root: "
            f"claim_authorities={{{key!r}: Authority({short!r}, module={path!r}, "
            "fail_safe=True)}"
        )
    return None


def _unexpected_issuer(
    path: str, expected: str | None, dropped: str | None, booted: str | None
) -> str:
    """Why ``path`` (``issues_tokens=True``) is not this map's issuer."""
    if expected is None and dropped is not None:
        return _dropped_issuer_problem(path, dropped, booted)
    named = (
        f"the claim_authorities issuer is {expected!r}"
        if expected is not None
        else "the claim_authorities map names no issuer"
    )
    return (
        f"module {path!r} declares issues_tokens=True, but {named}: name it as "
        f"the {ISSUER_ENTRY!r} entry, or drop issues_tokens"
    )


def _issuer_contributor_problem(issuer: str, critical: bool | None) -> str | None:
    """Decision 7 for the issuer fields: no other writer, no fallback."""
    fields = ", ".join(sorted(ISSUER_FIELDS))
    if critical is None:
        return (
            f"the claim_authorities issuer {issuer!r} registers no claims "
            f"contributor: the issuer fields ({fields}) have no other writer and "
            "no fallback (ADR-0014 decision 7); register one with "
            "add_claims_contributor(..., critical=True)"
        )
    if not critical:
        return (
            f"the claim_authorities issuer {issuer!r} registers its claims "
            f"contributor critical=False: the issuer fields ({fields}) have no "
            "fallback, so a token must never be minted without them (ADR-0014 "
            "decision 7); register the contributor critical=True"
        )
    return None


def _issuer_problems(
    authorities: ClaimAuthorities,
    loaded: Mapping[str, str],
    issuers: Iterable[str],
    contributors: Mapping[str, bool],
    *,
    dropped: str | None = None,
    booted: str | None = None,
) -> list[str]:
    """The issuer cross-check of an assigned map (adversarial X2), and the
    issuer's own contributor (decision 7)."""
    expected = authorities.issuer
    issuing = set(issuers)
    problems = [
        _unexpected_issuer(path, expected, dropped, booted)
        for path in sorted(issuing)
        if path != expected
    ]
    if expected is not None:
        short = module_name(expected)
        if loaded.get(short) == expected and expected not in issuing:
            problems.append(
                f"the claim_authorities issuer {expected!r} is loaded here but does "
                "not declare issues_tokens=True"
            )
        elif loaded.get(short) == expected:
            problem = _issuer_contributor_problem(expected, contributors.get(short))
            if problem is not None:
                problems.append(problem)
        elif short in loaded:
            problems.append(
                f"the claim_authorities issuer is {expected!r}, but the module "
                f"loaded here under the name {short!r} is {loaded[short]!r}"
            )
    return problems


def _booted(root: str | None) -> str:
    """The booted composition root, as a refusal names it."""
    if root:
        return f"the booted root {root!r} (SANDLOOM_COMPOSITION_ROOT)"
    return (
        f"the booted root {LEAN_ROOT!r} (the lean root: SANDLOOM_COMPOSITION_ROOT "
        "is unset)"
    )


def _installed_roots() -> list[tuple[str, Any]]:
    """``(root module, claim-authority map)`` of every installed root that
    registered its map under :data:`ROOTS_GROUP`. Read only to word a
    refusal (never at import, never on a request); a hint that does not load
    is skipped, so it never hides the refusal itself."""
    return [
        (entry.name, _load_hint(entry))
        for entry in metadata.entry_points().select(group=ROOTS_GROUP)
    ]


def _load_hint(entry: Any) -> Any:
    """The registered map, ``None`` when it does not load."""
    try:
        return entry.load()
    except (ImportError, AttributeError, ValueError):
        # A broken hint must not hide the refusal it words.
        return None


def _roots_composing(issuers: Iterable[str], booted: str | None) -> list[str]:
    """The installed roots, other than the booted one, whose map names one of
    ``issuers`` as its issuer (``sandloom_standard.main`` for
    ``sandloom_identity``): the kernel names no root, a root registers itself."""
    wanted = set(issuers)
    found: set[str] = set()
    for root, mapping in _installed_roots():
        if root == (booted or LEAN_ROOT) or not isinstance(mapping, Mapping):
            continue
        named = mapping.get(ISSUER_ENTRY)
        try:
            path = _entry_path(ISSUER_ENTRY, named)[0] if named else None
        except ClaimAuthorityError:
            continue
        if path in wanted:
            found.add(root)
    return sorted(found)


def _root_hint(issuers: Iterable[str], booted: str | None) -> str:
    roots = _roots_composing(issuers, booted)
    if not roots:
        return ""
    names = " or ".join(f"SANDLOOM_COMPOSITION_ROOT={root}" for root in roots)
    return f"; an installed root assigns this issuer's authorities: {names}"


def _no_map_problem(issuers: list[str], booted: str | None = None) -> str:
    """The refusal of an issuing process booted by a root with no map."""
    names = ", ".join(repr(path) for path in sorted(set(issuers)))
    return (
        f"no claim_authorities map is set, but {names} is loaded as the token "
        f"issuer (issues_tokens): the root that composes {names} must pass "
        "create_app(claim_authorities={...}) naming it as the "
        f"{ISSUER_ENTRY!r}, and SANDLOOM_COMPOSITION_ROOT must name that root; "
        f"{_booted(booted)} passes no map, so no protected claim (roles, "
        "permissions, tenant_ids, email, ...) has an authority and every token "
        f"would be refused (503){_root_hint(issuers, booted)}"
    )


def _dropped_issuer_problem(path: str, dropped: str, booted: str | None) -> str:
    """An issuer loaded under a default map whose issuer is not composed."""
    return (
        f"module {path!r} declares issues_tokens=True, but the default "
        f"claim-authority map of {_booted(booted)} names the issuer "
        f"{dropped!r}, which this composition does not load (a default map "
        "keeps only the authorities composed here): write your own composition "
        f"root with create_app(claim_authorities={{{ISSUER_ENTRY!r}: {path!r}, "
        "...}) and point SANDLOOM_COMPOSITION_ROOT at it"
    )


def claim_authority_problems(
    authorities: ClaimAuthorities,
    *,
    loaded: Mapping[str, str],
    contributors: Mapping[str, bool],
    bindings: Mapping[str, Mapping[str, Any]],
    issuers: Iterable[str],
    dropped_issuer: str | None = None,
    booted_root: str | None = None,
) -> list[str]:
    """Every reason the composition cannot honour ``authorities`` (empty: ok).

    ``loaded``: short name -> import path of every module loaded here;
    ``contributors``: short name -> critical, of every local claims owner;
    ``bindings``: ``SERVICE_BINDINGS``; ``issuers``: the import paths of the
    loaded ``issues_tokens=True`` modules (only an issuing process needs the
    authorities themselves). ``dropped_issuer``: the issuer of a default map
    that :func:`filter_to_composition` left out; ``booted_root``:
    ``SANDLOOM_COMPOSITION_ROOT`` (``None``: the lean root). Both only word
    a refusal.
    """
    issuer_list = list(issuers)
    if not authorities.assigned:
        return [_no_map_problem(issuer_list, booted_root)] if issuer_list else []
    problems: list[str] = []
    if issuer_list:
        # Only the issuing process mints, so only it needs every authority
        # loaded or bound; another process may not even have the module
        # installed (a split's own images).
        for key, path in sorted(authorities.authorities.items()):
            if key in ISSUER_FIELDS and path == authorities.issuer:
                # The issuer's own contributor writes them through the mint
                # check; it is checked as the issuer's, below.
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
    problems.extend(
        _issuer_problems(
            authorities,
            loaded,
            issuer_list,
            contributors,
            dropped=dropped_issuer,
            booted=booted_root,
        )
    )
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
    booted_root: str | None = None,
) -> ClaimAuthorities:
    """Resolve, check and assign the composition's claim authorities.

    Raises :class:`ConfigurationError` (SANDLOOM-B035) naming every problem;
    on success the facade's mint check applies the result, and the trusted
    view (:mod:`sandloom.core.auth.claims_view`) reads it from this moment on
    (``effective_at``). ``legacy_tokens`` turns on the bounded compatibility
    mode of the initial ``cpv`` enablement (G-RISK-14); past its window end
    it is off by itself and a WARNING asks to remove it. ``booted_root``
    (``SANDLOOM_COMPOSITION_ROOT``, ``None``: the lean root) only words a
    refusal.
    """
    descriptors = list(loaded_descriptors)
    loaded = {module_name(str(d.name)): str(d.name) for d in descriptors}
    issuers = [str(d.name) for d in descriptors if getattr(d, "issues_tokens", False)]
    dropped: str | None = None
    try:
        if claim_authorities is not None:
            authorities = build_claim_authorities(claim_authorities)
        elif default_claim_authorities is not None:
            default = build_claim_authorities(default_claim_authorities)
            authorities = filter_to_composition(
                default, loaded=loaded, bindings=bindings
            )
            if authorities.issuer is None:
                dropped = default.issuer
        else:
            authorities = NO_AUTHORITIES
        authorities = declared_policy(
            authorities,
            loaded=loaded,
            declared=core_facade._declared_fail_safe(),
            readers=_contract_check.generated_claim_keys(),
        )
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
            dropped_issuer=dropped,
            booted_root=booted_root,
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
    _recheck_claim_readers(core_facade, authorities, log)
    return authorities


def _unprotected_readers(authorities: ClaimAuthorities) -> list[str]:
    """The claim keys an imported generated claim atom reads that the
    composition's policy does not protect (empty: ok)."""
    return sorted(_contract_check.generated_claim_keys() - authorities.protected)


def _recheck_claim_readers(
    core_facade: Any, authorities: ClaimAuthorities, log: logging.Logger
) -> None:
    """A critical startup task, before the app serves: a generated claim
    atom imported after ``create_app`` reads a key the policy fixed at boot
    does not protect, so an unvouched value would reach it; refuse
    (SANDLOOM-B035) rather than serve. Boot only, never on a request."""

    async def recheck_claim_readers() -> None:
        late = _unprotected_readers(authorities)
        if late:
            message = (
                f"{CODE}: generated claim atoms imported after create_app read "
                f"{', '.join(late)}, which this composition's claim policy does "
                "not protect: import the generated contract copy before "
                f"create_app (from a module's code) (`sandloom explain {CODE}`)"
            )
            log.error("claim authorities: %s", message)
            raise UnprotectedClaimReader(message)

    core_facade.add_startup_task(recheck_claim_readers, critical=True, phase="infra")


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
    "LEAN_ROOT",
    "ROOTS_GROUP",
    "AuthorityEntry",
    "ClaimAuthorityError",
    "UnprotectedClaimReader",
    "build_claim_authorities",
    "claim_authority_problems",
    "declared_policy",
    "filter_to_composition",
    "install_claim_authorities",
    "legacy_until",
]
