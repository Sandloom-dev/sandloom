"""Claim authorities: the mint check, the violator rule and the startup
refusals (ADR-0014 decisions 1, 2 and 7; SANDLOOM-B035).

Kernel-only: the modules are fake import paths (``acme.*``) registered on a
facade with fake databases, so no battery is needed and no I/O happens. The
claim atoms ``Feature`` / ``WithinLimit`` are the ones ``sandloom generate``
writes beside a consumer's stub of a fixture plans contract
(``acme.plans.v1``, declared as billing declares them; ADR-0014 decision
15). The running-process rows (T-1..T-8, mono and split) are in
``tests/integration/replaceability/test_trust_rows.py``.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI

import sandloom.core.app_factory.claim_authorities as _ca
from sandloom.core.app_factory.claim_authorities import (
    ClaimAuthorityError,
    build_claim_authorities,
    claim_authority_problems,
    filter_to_composition,
    install_claim_authorities,
)
from sandloom.core.auth import claims_view as view
from sandloom.core.claims import (
    AUTHORITY_CLAIM,
    FAIL_SAFE_KEYS,
    ISSUER_FIELDS,
    NO_AUTHORITIES,
    PROTECTED_BASE,
    Authority,
    ClaimAuthorities,
)
from sandloom.core.cli.generated import render_stub
from sandloom.core.config.common import ConfigurationError
from sandloom.core.facade import ClaimsUnavailable, CoreFacade
from sandloom.core.facade._claims import CLAIMS_VIOLATIONS_METRIC, RESERVED_CLAIM_KEYS
from sandloom.core.tests._entry_points_support import (
    FakeEntryPoint,
    installed_entry_points,
)
from sandloom.sdk import contract
from sandloom.shared.authz import (
    Account,
    Category,
    From,
    Not,
    Or,
    Role,
    Subscribed,
    access_check,
    claim_atom,
)
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.subject import UserContext

ACME_ID = "acme.modules.acme_identity"
IDP = "acme.idp"
GRANTS = "acme.grants"
PLANS = "acme.plans"
ROGUE = "acme.rogue"

MAP = {
    "issuer": IDP,
    "roles": IDP,
    "permissions": IDP,
    "tenant_ids": GRANTS,
    "subscription_status": PLANS,
}


class _FakeUoW:
    def __init__(self) -> None:
        """A unit whose session has an empty ``info`` mapping."""
        self.session = SimpleNamespace(info={})

    async def __aenter__(self) -> _FakeUoW:
        """Enter the unit; nothing to open."""
        return self

    async def __aexit__(self, *exc: Any) -> None:
        """Leave the unit; nothing to commit."""
        return None


class _FakeDatabase:
    def uow_factory(self) -> Any:
        """The fake unit-of-work class."""
        return _FakeUoW

    def outbox(self) -> None:
        """No outbox."""
        return None

    def audit(self) -> None:
        """No audit sink."""
        return None


class _Proxy:
    """A remote ``<owner>.claims.v1`` client answering ``result``."""

    def __init__(self, result: dict[str, Any]) -> None:
        """Remember the answer and count the calls."""
        self.result = result
        self.calls = 0

    async def contribute(self, user_id: Any) -> dict[str, Any]:
        """Answer like ``<owner>.claims.v1.contribute`` over RPC."""
        self.calls += 1
        return self.result


class _Counted:
    """A contributor answering ``result`` and counting its calls."""

    def __init__(self, result: dict[str, Any]) -> None:
        """Remember the answer."""
        self.result = result
        self.calls = 0

    async def __call__(self, user_id: Any) -> dict[str, Any]:
        """Answer and count."""
        self.calls += 1
        return dict(self.result)


def _core(mapping: dict[str, str] | None = MAP) -> CoreFacade:
    core = CoreFacade(app=FastAPI())
    core.database = lambda name="default": _FakeDatabase()  # type: ignore[method-assign]
    if mapping is not None:  # else: as create_app would leave it, nothing
        core._assign_claim_authorities(build_claim_authorities(mapping))
    return core


def _register(
    core: CoreFacade, path: str, result: dict[str, Any], *, critical: bool = False
) -> _Counted:
    fn = _Counted(result)
    with core.module_registration(path) as api:
        api.add_claims_contributor(fn, critical=critical)
    return fn


async def _mint(core: CoreFacade) -> Any:
    return await core.collect_claims(uuid4())


# --------------------------------------------------------------------------- #
# the map
# --------------------------------------------------------------------------- #


def test_the_map_resolves_paths_short_names_and_the_issuer() -> None:
    """``issuer`` is kept apart; each key gets its authority's short name."""
    policy = build_claim_authorities(MAP)
    assert policy.issuer == IDP
    assert "issuer" not in policy.authorities
    assert policy.owner_of("roles") == "idp"
    assert policy.owner_of("tenant_ids") == "grants"
    assert policy.assigned
    assert policy.protected == PROTECTED_BASE | (set(MAP) - {"issuer"})


def test_the_issuer_fields_are_the_issuers() -> None:
    """A map with an issuer that names none assigns them to the issuer."""
    policy = build_claim_authorities(MAP)
    for key in ISSUER_FIELDS:
        assert policy.owner_of(key) == "idp"
        assert policy.authorities[key] == IDP
    explicit = build_claim_authorities({**MAP, "email": IDP})
    assert explicit.authorities == policy.authorities


@pytest.mark.parametrize("key", sorted(ISSUER_FIELDS))
@pytest.mark.parametrize(
    "mapping",
    [{**MAP, "{key}": PLANS}, {"roles": IDP, "{key}": IDP}],
    ids=["another-module", "no-issuer"],
)
def test_an_issuer_field_assigned_to_anyone_else_is_refused(
    key: str, mapping: dict[str, str]
) -> None:
    """Never silently overwritten by the issuer: the map is refused."""
    entry = {k.format(key=key): v for k, v in mapping.items()}
    with pytest.raises(ClaimAuthorityError, match=f"{key!r} is an issuer field"):
        build_claim_authorities(entry)


def test_the_map_is_read_only() -> None:
    """One-shot is enforced: the checked map cannot be edited in place."""
    source = dict(MAP)
    policy = build_claim_authorities(source)
    with pytest.raises(TypeError):
        policy.authorities["roles"] = ROGUE  # type: ignore[index]
    with pytest.raises(TypeError):
        policy.owners["roles"] = "rogue"  # type: ignore[index]
    owners = {"roles": "idp"}
    built = ClaimAuthorities(authorities={"roles": IDP}, owners=owners, assigned=True)
    owners["roles"] = "rogue"
    assert built.owner_of("roles") == "idp"


def test_no_map_assigns_nothing_and_protects_the_base_keys() -> None:
    """A lean root: nothing assigned, the kernel's keys still protected."""
    assert build_claim_authorities(None) is NO_AUTHORITIES
    assert not NO_AUTHORITIES.assigned
    assert {"roles", "permissions", "tenant_ids", "email"} <= NO_AUTHORITIES.protected
    assert FAIL_SAFE_KEYS <= NO_AUTHORITIES.protected


@pytest.mark.parametrize("bad", [{"roles": ""}, {"": IDP}, {"roles": 3}])
def test_a_malformed_entry_is_refused(bad: dict[str, Any]) -> None:
    """Each entry maps a claim key to a module name or path."""
    with pytest.raises(ClaimAuthorityError):
        build_claim_authorities(bad)


def test_only_fail_safe_keys_may_have_a_non_critical_authority() -> None:
    """Decision 7: ``Role``/``Perm``/``TenantScope`` keys need a critical one."""
    for key in ("roles", "permissions", "tenant_ids", "email", "loyalty_tier"):
        assert ClaimAuthorities.critical_required(key)
    for key in FAIL_SAFE_KEYS:
        assert not ClaimAuthorities.critical_required(key)


# --------------------------------------------------------------------------- #
# the mint check and the violator rule
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_authorities_keep_their_claims() -> None:
    """Assigned authorities write their keys; provenance as before."""
    core = _core()
    _register(core, IDP, {"roles": ["admin"], "permissions": ["p"]}, critical=True)
    _register(core, GRANTS, {"tenant_ids": ["t"]}, critical=True)
    claims = await _mint(core)
    assert claims["roles"] == ["admin"]
    assert claims["tenant_ids"] == ["t"]
    assert claims["claims_sources"] == ["grants", "idp"]
    assert claims["claims_sources_failed"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first", [True, False], ids=["violator-first", "violator-last"]
)
@pytest.mark.parametrize(
    "payload",
    [
        {"roles": ["admin"]},
        {"permissions": ["p"]},
        {"tenant_ids": ["foreign"]},
        {"subscription_status": "active"},
        {"email": "x@y"},
        {"claims_sources": ["idp"]},
        {"tenant_id": "foreign"},
        {"jti": "x"},
    ],
)
async def test_an_optional_violator_loses_its_whole_result(
    payload: dict[str, Any], first: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """T-2, T-4: the authorities' values only; the violator in failed, ERROR, +1."""
    core = _core()
    rogue_payload = {**payload, "loyalty_tier": "gold"}
    if first:
        _register(core, ROGUE, rogue_payload)
    _register(core, IDP, {"roles": [], "permissions": []}, critical=True)
    _register(core, GRANTS, {"tenant_ids": []}, critical=True)
    if not first:
        _register(core, ROGUE, rogue_payload)
    with caplog.at_level(logging.ERROR, logger="sandloom.core.facade"):
        claims = await _mint(core)
    assert claims["roles"] == []
    assert claims["tenant_ids"] == []
    assert "loyalty_tier" not in claims  # never a partial result
    assert "tenant_id" not in claims
    assert "jti" not in claims
    assert claims["claims_sources"] == ["grants", "idp"]
    assert claims["claims_sources_failed"] == ["rogue"]
    assert core.get_metric(CLAIMS_VIOLATIONS_METRIC) == 1
    (record,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert "'rogue'" in record.getMessage()
    assert repr(next(iter(payload))) in record.getMessage()
    assert "whole contribution is dropped" in record.getMessage()


@pytest.mark.asyncio
async def test_a_hundred_violations_mint_a_hundred_tokens() -> None:
    """T-4: one buggy optional module never denies a login (problem #14)."""
    core = _core()
    _register(core, IDP, {"roles": [], "permissions": []}, critical=True)
    _register(core, ROGUE, {"roles": ["admin"], "jti": "x"})
    tokens = [await _mint(core) for _ in range(100)]
    assert sum("admin" in t["roles"] for t in tokens) == 0
    assert core.get_metric(CLAIMS_VIOLATIONS_METRIC) == 100


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first", [True, False], ids=["violator-first", "violator-last"]
)
async def test_a_critical_violator_refuses_the_mint_naming_itself(first: bool) -> None:
    """T-3: 503 naming the violator, never the authority, in either order."""
    core = _core()
    if first:
        _register(core, ROGUE, {"roles": ["admin"]}, critical=True)
    _register(core, IDP, {"roles": []}, critical=True)
    if not first:
        _register(core, ROGUE, {"roles": ["admin"]}, critical=True)
    with pytest.raises(ClaimsUnavailable) as refused:
        await _mint(core)
    assert refused.value.status_code == 503
    assert refused.value.owner == "rogue"


@pytest.mark.asyncio
async def test_a_contributor_writing_only_its_own_keys_contributes() -> None:
    """An unprotected key of one owner still merges."""
    core = _core()
    _register(core, IDP, {"roles": []}, critical=True)
    _register(core, ROGUE, {"loyalty_tier": "gold"})
    claims = await _mint(core)
    assert claims["loyalty_tier"] == "gold"
    assert claims["claims_sources"] == ["idp", "rogue"]


@pytest.mark.asyncio
async def test_an_unprotected_key_keeps_one_owner_per_token() -> None:
    """PR #70's rule for other keys: the second writer is the violator."""
    core = _core()
    _register(core, IDP, {"roles": [], "loyalty_tier": "a"}, critical=True)
    _register(core, ROGUE, {"loyalty_tier": "b"})
    claims = await _mint(core)
    assert claims["loyalty_tier"] == "a"
    assert claims["claims_sources_failed"] == ["rogue"]


@pytest.mark.asyncio
async def test_without_a_map_no_writer_of_a_protected_key_is_trusted() -> None:
    """T-6: optional writers dropped; a critical issuer refuses every mint."""
    core = _core(None)
    _register(core, ROGUE, {"roles": ["admin"], "tenant_ids": ["t"]})
    claims = await _mint(core)
    assert "roles" not in claims
    assert "tenant_ids" not in claims
    assert claims["claims_sources_failed"] == ["rogue"]
    _register(core, IDP, {"roles": ["admin"]}, critical=True)
    with pytest.raises(ClaimsUnavailable) as refused:
        await _mint(core)
    assert refused.value.owner == "idp"


@pytest.mark.asyncio
async def test_a_module_named_like_the_authority_is_not_it() -> None:
    """T-7: the same short name from another path is not the assigned module."""
    core = _core({"roles": "acme.idp"})
    _register(core, "other.idp", {"roles": ["admin"]})
    claims = await _mint(core)
    assert "roles" not in claims
    assert claims["claims_sources_failed"] == ["idp"]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", sorted(RESERVED_CLAIM_KEYS))
async def test_a_reserved_key_is_nobodys(key: str) -> None:
    """Even the authority loses its result when it writes a reserved key."""
    core = _core()
    _register(core, IDP, {"roles": ["admin"], key: 9_999_999_999})
    claims = await _mint(core)
    assert "roles" not in claims
    assert claims["claims_sources_failed"] == ["idp"]


@pytest.mark.asyncio
async def test_a_remote_authority_is_its_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Split: ``grants.claims.v1`` writes ``tenant_ids``; ``rogue.claims.v1`` cannot."""
    core = _core()
    _register(core, IDP, {"roles": []}, critical=True)
    monkeypatch.setattr(
        core.settings,
        "service_bindings",
        {
            "grants.claims.v1": {"base_url": "http://grants"},
            "rogue.claims.v1": {"base_url": "http://rogue", "critical": False},
        },
    )
    proxies = {
        "grants.claims.v1": _Proxy({"tenant_ids": ["t"], AUTHORITY_CLAIM: GRANTS}),
        "rogue.claims.v1": _Proxy({"roles": ["admin"], "tenant_ids": ["x"]}),
    }
    core.set_service_resolver(lambda name, binding, **_: proxies[name])
    claims = await _mint(core)
    assert claims["roles"] == []
    assert claims["tenant_ids"] == ["t"]
    assert claims["claims_sources"] == ["grants", "idp"]
    assert claims["claims_sources_failed"] == ["rogue"]


@pytest.mark.asyncio
async def test_the_mint_check_adds_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """M-calls: one call per owner, violators included; nothing else runs."""
    core = _core()
    idp = _register(core, IDP, {"roles": []}, critical=True)
    rogue = _register(core, ROGUE, {"roles": ["admin"]})
    monkeypatch.setattr(
        core.settings,
        "service_bindings",
        {"grants.claims.v1": {"base_url": "http://grants"}},
    )
    proxy = _Proxy({"tenant_ids": [], AUTHORITY_CLAIM: GRANTS})
    core.set_service_resolver(lambda name, binding, **_: proxy)
    await _mint(core)
    assert (idp.calls, rogue.calls, proxy.calls) == (1, 1, 1)


@pytest.mark.asyncio
async def test_a_failed_non_critical_fail_safe_authority_still_mints() -> None:
    """Billing option (c): its outage leaves a login with ``failed`` provenance."""
    core = _core()
    _register(core, IDP, {"roles": []}, critical=True)

    async def down(user_id: Any) -> dict[str, Any]:
        """The plans authority is down."""
        raise RuntimeError("plans down")

    with core.module_registration(PLANS) as api:
        api.add_claims_contributor(down)
    claims = await _mint(core)
    assert "subscription_status" not in claims
    assert claims["claims_sources_failed"] == ["plans"]


# --------------------------------------------------------------------------- #
# startup refusals (SANDLOOM-B035)
# --------------------------------------------------------------------------- #

LOADED = {"idp": IDP, "grants": GRANTS, "plans": PLANS}
CONTRIBUTORS = {"idp": True, "grants": True, "plans": False}


def _problems(
    mapping: dict[str, str],
    *,
    loaded: dict[str, str] = LOADED,
    contributors: dict[str, bool] = CONTRIBUTORS,
    bindings: dict[str, Any] | None = None,
    issuers: tuple[str, ...] = (IDP,),
) -> list[str]:
    return claim_authority_problems(
        build_claim_authorities(mapping),
        loaded=loaded,
        contributors=contributors,
        bindings=bindings or {},
        issuers=issuers,
    )


def test_a_composition_that_honours_the_map_passes() -> None:
    """Local authorities, critical where required; the plans one non-critical."""
    assert _problems(MAP) == []


def test_startup_names_a_typo_of_a_loaded_authority() -> None:
    """T-5: an assigned path close to a loaded one is named as its typo."""
    (problem,) = _problems({**MAP, "roles": "acme.ipd"})
    assert "claim 'roles' is assigned to 'acme.ipd', which is neither loaded" in problem
    assert "a typo of 'acme.idp'?" in problem


def test_the_typo_hint_imports_nothing() -> None:
    """Only loaded paths are compared; an unrelated path gets no hint."""
    assert _ca._typo_hint("acme.ipd", LOADED) == "; a typo of 'acme.idp'?"
    assert _ca._typo_hint("zzz.unrelated", LOADED) == ""


def test_startup_refuses_an_authority_neither_loaded_nor_bound() -> None:
    """T-5: the issuing process cannot reach the authority."""
    (problem,) = _problems(MAP, loaded={"idp": IDP, "plans": PLANS})
    assert (
        "claim 'tenant_ids' is assigned to 'acme.grants', which is neither" in problem
    )
    assert "grants.claims.v1 is not in SERVICE_BINDINGS" in problem


def test_startup_refuses_a_non_critical_authority_of_a_rights_key() -> None:
    """T-5 / decision 7: ``roles`` from a critical=False contributor."""
    problems = _problems(MAP, contributors={**CONTRIBUTORS, "idp": False})
    # roles, permissions, and the issuer's own fields (email, email_verified)
    assert len(problems) == 3
    assert all("critical=False" in p for p in problems)


def test_startup_refuses_a_non_critical_binding_of_a_rights_key() -> None:
    """Split: ``grants.claims.v1`` with ``critical: false`` writing tenant_ids."""
    (problem,) = _problems(
        MAP,
        loaded={"idp": IDP, "plans": PLANS},
        bindings={"grants.claims.v1": {"base_url": "http://g", "critical": False}},
    )
    assert "whose binding grants.claims.v1 is critical: false" in problem


def test_a_non_critical_fail_safe_authority_is_accepted_local_and_bound() -> None:
    """Billing option (c), mono and split."""
    assert _problems(MAP) == []
    assert (
        _problems(
            MAP,
            loaded={"idp": IDP, "grants": GRANTS},
            bindings={"plans.claims.v1": {"base_url": "http://p", "critical": False}},
        )
        == []
    )


def test_startup_refuses_an_authority_without_a_contributor() -> None:
    """A loaded authority that writes nothing cannot vouch for its key."""
    (problem,) = _problems(MAP, contributors={"idp": True, "plans": False})
    assert "'acme.grants'" in problem
    assert "registers no claims contributor" in problem


def test_startup_refuses_a_module_of_the_same_name_from_another_path() -> None:
    """T-7: the short name is not proof of the authority."""
    (problem,) = _problems(MAP, loaded={**LOADED, "grants": "other.grants"})
    assert "the module loaded here under the name 'grants' is 'other.grants'" in problem


def test_startup_refuses_an_issuer_that_is_not_the_maps() -> None:
    """X2: an ``issues_tokens`` module that is not the map's issuer."""
    problems = _problems({**MAP, "issuer": GRANTS}, issuers=(IDP,))
    assert any("module 'acme.idp' declares issues_tokens=True" in p for p in problems)
    assert any(
        "issuer 'acme.grants' is loaded here but does not declare" in p
        for p in problems
    )


def test_startup_refuses_the_maps_issuer_without_issues_tokens() -> None:
    """X2, the reverse: the map's issuer loaded but not issuing."""
    assert _problems(MAP, issuers=()) == [
        "the claim_authorities issuer 'acme.idp' is loaded here but does not "
        "declare issues_tokens=True"
    ]


def test_a_map_without_issuer_refuses_a_loaded_issuer() -> None:
    """An assigned map with no ``issuer`` entry and an issuing module."""
    mapping = {k: v for k, v in MAP.items() if k != "issuer"}
    (problem,) = _problems(mapping)
    assert "but the claim_authorities map names no issuer" in problem
    assert "is None" not in problem


def test_a_non_issuing_process_checks_only_the_issuer() -> None:
    """A split's provider process neither mints nor needs the authorities."""
    assert _problems(MAP, loaded={"plans": PLANS}, issuers=()) == []


def test_no_map_refuses_an_issuing_process_only() -> None:
    """A lean root with an issuer refuses the boot rather than 503 every
    login; a process minting nothing boots."""
    (problem,) = claim_authority_problems(
        NO_AUTHORITIES, loaded=LOADED, contributors={}, bindings={}, issuers=[IDP]
    )
    assert "no claim_authorities map is set, but 'acme.idp' is loaded as" in problem
    assert "create_app(claim_authorities={...})" in problem
    assert "SANDLOOM_COMPOSITION_ROOT" in problem
    assert (
        claim_authority_problems(
            NO_AUTHORITIES, loaded=LOADED, contributors={}, bindings={}, issuers=[]
        )
        == []
    )


def _roots(monkeypatch: pytest.MonkeyPatch, *roots: tuple[str, Any]) -> None:
    """The installed roots' registered maps (``sandloom.composition_roots``)."""
    monkeypatch.setattr(_ca, "_installed_roots", lambda: list(roots))


def test_no_map_leads_with_the_root_that_composes_the_issuer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 3: the fix comes first, the booted root is named, and no
    installed root is suggested when none registered a map naming this
    issuer (the kernel names no root of its own accord)."""
    _roots(monkeypatch, ("std.main", {"issuer": "acme.other_idp"}))
    for booted, named in (
        (None, "the booted root 'sandloom.main' (the lean root"),
        ("acme.asgi", "the booted root 'acme.asgi' (SANDLOOM_COMPOSITION_ROOT)"),
    ):
        (problem,) = claim_authority_problems(
            NO_AUTHORITIES,
            loaded=LOADED,
            contributors={},
            bindings={},
            issuers=[IDP],
            booted_root=booted,
        )
        assert problem.index("the root that composes 'acme.idp' must pass") < (
            problem.index(named)
        )
        assert "std.main" not in problem
        assert "sandloom_standard" not in problem


def test_no_map_suggests_a_registered_root_whose_map_names_the_issuer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A root that registered its map (``sandloom_standard.main`` does) is
    suggested only for the issuer that map names, never for itself."""
    _roots(
        monkeypatch,
        ("std.main", {"issuer": IDP, "roles": IDP}),
        ("other.main", {"issuer": "acme.other_idp"}),
        ("broken.main", ["not", "a", "map"]),
        ("acme.asgi", {"issuer": IDP}),
    )
    (problem,) = claim_authority_problems(
        NO_AUTHORITIES,
        loaded=LOADED,
        contributors={},
        bindings={},
        issuers=[IDP],
        booted_root="acme.asgi",
    )
    assert problem.endswith(
        "an installed root assigns this issuer's authorities: "
        "SANDLOOM_COMPOSITION_ROOT=std.main"
    )
    assert "other.main" not in problem


def test_a_broken_root_hint_never_hides_the_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry point that fails to load is skipped, the refusal stands."""

    class _Broken:
        name = "broken.main"

        def load(self) -> Any:
            raise ImportError("gone")

    monkeypatch.setattr(
        _ca.metadata,
        "entry_points",
        lambda: SimpleNamespace(select=lambda group: [_Broken()]),
    )
    (problem,) = claim_authority_problems(
        NO_AUTHORITIES, loaded=LOADED, contributors={}, bindings={}, issuers=[IDP]
    )
    assert "no claim_authorities map is set" in problem
    assert "installed root" not in problem


def test_the_issuer_needs_a_critical_contributor_of_its_own() -> None:
    """Decision 7: the issuer fields have no other writer and no fallback,
    so an issuer without a contributor, or with an optional one, is refused
    (its every invitation accept would otherwise fail closed)."""
    (absent,) = _problems({"issuer": IDP}, contributors={})
    assert "issuer 'acme.idp' registers no claims contributor" in absent
    (optional,) = _problems({"issuer": IDP}, contributors={"idp": False})
    assert "registers its claims contributor critical=False" in optional
    assert "email, email_verified" in optional
    assert _problems({"issuer": IDP}, contributors={"idp": True}) == []
    assert _problems({"issuer": IDP, "email": IDP}, contributors={"idp": True}) == []


def test_only_the_issuing_process_checks_the_issuers_contributor() -> None:
    """A split's other process neither loads the issuer nor mints."""
    assert _problems({"issuer": IDP}, loaded={}, contributors={}, issuers=()) == []


def test_a_left_out_default_issuer_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """Finding 4: a default map whose issuer is not composed here, with
    another issuer loaded, names the left-out issuer, not "issuer is None"."""
    core = _core(None)
    acme = "acme.oidc"
    _register(core, acme, {}, critical=True)
    with pytest.raises(ConfigurationError) as refused:
        install_claim_authorities(
            core,
            [_descriptor(acme, issues_tokens=True)],
            claim_authorities=None,
            default_claim_authorities={"issuer": IDP, "roles": IDP},
            bindings={},
            log=logging.getLogger("test"),
            booted_root="std.main",
        )
    message = str(refused.value)
    assert "SANDLOOM-B035" in message
    assert (
        "the default claim-authority map of the booted root 'std.main' "
        "(SANDLOOM_COMPOSITION_ROOT) names the issuer 'acme.idp', which this "
        "composition does not load"
    ) in message
    assert "create_app(claim_authorities={'issuer': 'acme.oidc', ...})" in message
    assert "is None" not in message


def test_the_default_map_keeps_only_the_composed_authorities() -> None:
    """ADR-0012 decision 3: loaded or bound; the issuer only when loaded."""
    policy = filter_to_composition(
        build_claim_authorities(MAP),
        loaded={"grants": GRANTS},
        bindings={"plans.claims.v1": {"base_url": "http://p"}},
    )
    assert dict(policy.authorities) == {
        "tenant_ids": GRANTS,
        "subscription_status": PLANS,
    }
    assert policy.issuer is None
    assert policy.owner_of("roles") is None
    assert policy.assigned


def _descriptor(path: str, *, issues_tokens: bool = False) -> Any:
    return SimpleNamespace(name=path, issues_tokens=issues_tokens)


def test_install_assigns_the_checked_map_and_the_explicit_map_wins() -> None:
    """``create_app``'s step: the facade's mint check gets the result."""
    core = _core(None)
    _register(core, IDP, {"roles": []}, critical=True)
    policy = install_claim_authorities(
        core,
        [_descriptor(IDP, issues_tokens=True)],
        claim_authorities={"issuer": IDP, "roles": IDP},
        default_claim_authorities={"roles": ROGUE},
        bindings={},
        log=logging.getLogger("test"),
    )
    assert core.claim_authorities is policy
    assert policy.owner_of("roles") == "idp"


def test_install_refuses_with_b035_naming_every_problem() -> None:
    """One ``ConfigurationError`` listing each problem, with the code."""
    core = _core(None)
    _register(core, IDP, {"roles": []})
    with pytest.raises(ConfigurationError) as refused:
        install_claim_authorities(
            core,
            [_descriptor(IDP, issues_tokens=True)],
            claim_authorities={"issuer": IDP, "roles": IDP, "tenant_ids": "nowhere"},
            default_claim_authorities=None,
            bindings={},
            log=logging.getLogger("test"),
        )
    message = str(refused.value)
    assert "SANDLOOM-B035" in message
    assert "'nowhere', which is neither loaded" in message
    assert "critical=False" in message
    assert not core.claim_authorities.assigned


def test_the_assignment_is_made_once() -> None:
    """Code running in the composition cannot re-assign the trust map."""
    core = _core()
    with pytest.raises(RuntimeError, match="assigned once"):
        core._assign_claim_authorities(build_claim_authorities({"roles": ROGUE}))
    assert core.claim_authorities.owner_of("roles") == "idp"


def test_install_without_a_map_refuses_an_issuing_process() -> None:
    """The lean root with the batteries: B035 at boot, not a 503 per login."""
    core = _core(None)
    _register(core, IDP, {"roles": []}, critical=True)
    with pytest.raises(ConfigurationError) as refused:
        install_claim_authorities(
            core,
            [_descriptor(IDP, issues_tokens=True)],
            claim_authorities=None,
            default_claim_authorities=None,
            bindings={},
            log=logging.getLogger("test"),
        )
    message = str(refused.value)
    assert "SANDLOOM-B035" in message
    assert "no claim_authorities map is set" in message
    assert "sandloom explain SANDLOOM-B035" in message


def test_install_without_a_map_boots_a_process_minting_nothing() -> None:
    """No issuer loaded: nothing to assign, the boot goes on."""
    core = _core(None)
    policy = install_claim_authorities(
        core,
        [_descriptor(PLANS)],
        claim_authorities=None,
        default_claim_authorities=None,
        bindings={},
        log=logging.getLogger("test"),
    )
    assert policy.authorities == {} and not policy.assigned and policy.issuer is None


def test_install_refuses_an_issuer_field_given_to_another_module() -> None:
    """The finding: ``email`` assigned to billing no longer boots."""
    core = _core(None)
    _register(core, IDP, {"roles": []}, critical=True)
    with pytest.raises(ConfigurationError) as refused:
        install_claim_authorities(
            core,
            [_descriptor(IDP, issues_tokens=True)],
            claim_authorities={**MAP, "email": PLANS},
            default_claim_authorities=None,
            bindings={},
            log=logging.getLogger("test"),
        )
    assert "SANDLOOM-B035" in str(refused.value)
    assert "'email' is an issuer field" in str(refused.value)


# --------------------------------------------------------------------------- #
# the issuer's own fields at the mint: its contributor's, through the check
# --------------------------------------------------------------------------- #

FIELDS = {"email": "u@example.com", "email_verified": False}


async def _issue(core: CoreFacade, fields: dict[str, Any] = FIELDS) -> Any:
    """Collect as the issuer ``acme.idp``, whose own contributor returns
    ``fields`` (registered here when the test did not)."""
    mine = core._contributors_of("idp") or [_register(core, IDP, fields, critical=True)]
    return await core.collect_claims(uuid4(), issuer=mine[0])


@pytest.mark.asyncio
async def test_the_issuers_fields_pass_the_check_and_win() -> None:
    """Kept from the issuer's contributor; another one writing one is a
    violator and loses its whole result."""
    core = _core()
    _register(core, IDP, {"roles": ["member"], **FIELDS}, critical=True)
    _register(core, ROGUE, {"email_verified": True, "loyalty": 1})
    claims = await _issue(core)
    assert claims["email"] == "u@example.com"
    assert claims["email_verified"] is False
    assert "loyalty" not in claims
    assert claims["claims_sources_failed"] == ["rogue"]


@pytest.mark.asyncio
async def test_without_a_map_the_issuers_fields_are_not_minted() -> None:
    """No map: no authority is assigned the issuer's fields, so the critical
    contributor writing them refuses the mint (never a token carrying them)."""
    core = _core(None)
    with pytest.raises(ClaimsUnavailable) as refused:
        await _issue(core)
    assert refused.value.owner == "idp"


@pytest.mark.asyncio
async def test_another_module_cannot_mint_the_issuer_fields() -> None:
    """Only the map's issuer's: another module naming its own contributor as
    the issuer still writes them as a violator."""
    core = _core()
    _register(core, IDP, {"roles": [], **FIELDS}, critical=True)
    rogue = _register(core, ROGUE, {"email": "eve@example.com"})
    claims = await core.collect_claims(uuid4(), issuer=rogue)
    assert claims["email"] == "u@example.com"
    assert claims["claims_sources_failed"] == ["rogue"]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["sub", "claims_sources", "cpv"])
async def test_the_issuer_writes_no_reserved_key(key: str) -> None:
    """The kernel's own fields are nobody's, the issuer's included."""
    core = _core()
    with pytest.raises(ClaimsUnavailable):
        await _issue(core, {**FIELDS, key: "x"})


# --------------------------------------------------------------------------- #
# Feature / WithinLimit: a failed authority is indeterminate (decision 7)
# --------------------------------------------------------------------------- #

PLAN_MAP = {**MAP, "features": PLANS, "limits": PLANS}
PLANS_CONTRACT = "acme.plans.v1"
ACCOUNT_BIND = {"tenant_type": From.account_type(), "tenant_id": From.account_id()}


@contract(PLANS_CONTRACT)
class PlansContract:
    """Plan entitlements (a fixture provider, declared as billing declares
    its ``Feature`` / ``WithinLimit``)."""

    @claim_atom("Feature", claim_key="features", entry="key")
    @access_check("LiveFeature", bind=ACCOUNT_BIND, describe="feature '{key}'")
    async def has_feature(self, tenant_type: str, tenant_id: UUID, key: str) -> bool:
        """True when the plan enables ``key``."""
        return False

    @claim_atom("WithinLimit", claim_key="limits", entry="key", below="used")
    @access_check(
        "LiveWithinLimit",
        bind=ACCOUNT_BIND,
        category=Category.QUOTA_EXCEEDED,
        describe="within limit '{key}'",
    )
    async def is_within_limit(
        self, tenant_type: str, tenant_id: UUID, key: str, used: int
    ) -> bool:
        """True when ``used`` is below the plan limit ``key``."""
        return False


@pytest.fixture(scope="module")
def gen(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    """The generated atoms of :data:`PLANS_CONTRACT`, imported as a consumer
    imports them."""
    path = tmp_path_factory.mktemp("stub") / "acme_plans_v1.py"
    path.write_text(render_stub(PLANS_CONTRACT, PlansContract))
    spec = importlib.util.spec_from_file_location("_trust_acme_plans_v1", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def _subject(*, failed: set[str] | None, **claims: Any) -> UserContext:
    data: dict[str, Any] = {"user_id": uuid4(), "roles": {"member"}, **claims}
    if failed is not None:
        data["claims_sources"] = {"idp"}
        data["claims_sources_failed"] = failed
    return UserContext(**data)


def _authz(subject: UserContext, mapping: dict[str, str] | None = PLAN_MAP) -> Any:
    core = _core(mapping) if mapping is not None else None
    return AuthzContext(subject, core=core)


#: The generated atom of each kind, over the generated module.
ATOMS: dict[str, Callable[[Any], Any]] = {
    "Feature": lambda g: g.Feature("export", of=Account.user()),
    "WithinLimit": lambda g: g.WithinLimit("seats", 2, of=Account.user()),
}
KINDS = sorted(ATOMS)


def _feature(gen: Any) -> Any:
    return ATOMS["Feature"](gen)


def _unknown(decision: Any) -> bool:
    """Indeterminate 501 ``access_unknown``: the token says nothing."""
    return (
        not decision.allowed
        and decision.indeterminate
        and (decision.http_status, decision.code) == (501, "access_unknown")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_a_failed_authority_is_indeterminate_503(gen: Any, kind: str) -> None:
    """The plans authority failed at the mint: the token proves nothing."""
    decision = await ATOMS[kind](gen).decide(_authz(_subject(failed={"plans"})))
    assert not decision.allowed
    assert decision.indeterminate
    assert decision.http_status == 503
    assert decision.code == "claim_authority_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_an_authority_that_ran_denies_only_on_its_complete_map(
    gen: Any, kind: str
) -> None:
    """The plans authority ran: its mapping is the complete snapshot, so an
    empty one is a definite "no"; no mapping at all is no word from it,
    indeterminate 501 ``access_unknown`` (KB-33d), and ``Not`` never allows."""
    atom = ATOMS[kind](gen)
    empty = _authz(_subject(failed=set(), features={}, limits={}))
    decision = await atom.decide(empty)
    assert not decision.allowed and not decision.indeterminate
    assert (await Not(atom).decide(empty)).allowed
    for subject in (_subject(failed=set()), _subject(failed={"rogue"})):
        ctx = _authz(subject)
        assert _unknown(await atom.decide(ctx))
        assert _unknown(await Not(atom).decide(ctx))


@pytest.mark.asyncio
async def test_a_present_claim_is_read_whatever_failed(gen: Any) -> None:
    """Only an absent value escalates."""
    subject = _subject(failed={"rogue"}, features={"export": True}, limits={"seats": 3})
    for kind in KINDS:
        assert (await ATOMS[kind](gen).decide(_authz(subject))).allowed, kind


@pytest.mark.asyncio
async def test_an_unassigned_key_and_a_legacy_token_are_unknown(gen: Any) -> None:
    """Unassigned in the composition, or no provenance: nobody vouched for an
    absent claim, 501 ``access_unknown``, never a deny ``Not`` could invert."""
    for ctx in (
        _authz(_subject(failed={"plans"}), MAP),
        _authz(_subject(failed=None)),
    ):
        assert _unknown(await _feature(gen).decide(ctx))
        assert _unknown(await Not(_feature(gen)).decide(ctx))


@pytest.mark.asyncio
async def test_without_a_readable_assignment_a_failure_is_indeterminate(
    gen: Any,
) -> None:
    """No core (or a root without a map): any failed source may have been the
    authority, so never a deny ``Not`` could invert."""
    for ctx in (
        _authz(_subject(failed={"plans"}), None),
        AuthzContext(_subject(failed={"plans"}), core=_core(None)),
    ):
        decision = await _feature(gen).decide(ctx)
        assert decision.indeterminate and decision.http_status == 503


@pytest.mark.asyncio
async def test_not_and_or_never_allow_on_a_failed_authority(gen: Any) -> None:
    """ADR-0014: an indeterminate operand never allows; a satisfied one does."""
    ctx = _authz(_subject(failed={"plans"}))
    feature = _feature(gen)
    negated = await Not(feature).decide(ctx)
    assert not negated.allowed and negated.indeterminate
    either = await Or(Role("ops"), feature).decide(ctx)
    assert not either.allowed and either.http_status == 503
    assert (await Or(Role("member"), feature).decide(ctx)).allowed


# --------------------------------------------------------------------------- #
# SEC-4: a split verifier reads the authority from the verifier's map
# --------------------------------------------------------------------------- #


def _split_verifier() -> CoreFacade:
    """A service running neither the plans authority nor the issuer: its
    mint map is narrowed to what it composes, the verifier's map is whole."""
    core = CoreFacade(app=FastAPI())
    core._assign_claim_authorities(
        filter_to_composition(build_claim_authorities(PLAN_MAP), loaded={}, bindings={})
    )
    return core


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_a_split_verifier_without_the_authority_is_indeterminate(
    gen: Any, kind: str
) -> None:
    """The plans authority failed at the mint and runs in another service:
    the token proves nothing here either, so 503, and ``Not`` never allows
    (the narrowed mint map would read the key as unassigned: a deny that
    ``~Feature`` turns into an allow)."""
    ctx = AuthzContext(_subject(failed={"plans"}), core=_split_verifier())
    atom = ATOMS[kind](gen)
    decision = await atom.decide(ctx)
    assert decision.indeterminate and decision.http_status == 503
    negated = await Not(atom).decide(ctx)
    assert not negated.allowed and negated.indeterminate


# --------------------------------------------------------------------------- #
# SEC-3: Subscribed / Feature / WithinLimit honour the view's 401 / 503
# --------------------------------------------------------------------------- #

#: (the atom over the generated module, the claim key it reads). Feature and
#: WithinLimit are the generated atoms. Subscribed is still the kernel atom
#: here: the generated ``Subscribed`` (a ``ClaimFactCheck``) does not honour
#: the view's 401 / 503 yet (it denies 402, so ``Not`` allows); its rows
#: move once it does (KB-34b handover).
CLAIM_ATOMS: list[tuple[Callable[[Any], Any], str]] = [
    (ATOMS["Feature"], "features"),
    (ATOMS["WithinLimit"], "limits"),
    (lambda _g: Subscribed(of=Account.user()), "subscription_status"),
    (
        lambda _g: Subscribed(frozenset({"active"}), of=Account.user()),
        "subscription_status",
    ),
]
CLAIM_IDS = ["Feature", "WithinLimit", "Subscribed", "Subscribed-statuses"]
#: The keys the generated claim atoms read: a claim the view removed is no
#: source's word for them.
GENERATED_KEYS = frozenset({"features", "limits"})


def _viewed(key: str, reason: str) -> UserContext:
    """A subject whose ``key`` the trusted view removed for ``reason``."""
    return UserContext(
        user_id=uuid4(),
        claims_sources={"idp", "plans"},
        claims_sources_failed=set(),
        **{view.UNTRUSTED_KEY: {key: reason}},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("atom", "key"), CLAIM_ATOMS, ids=CLAIM_IDS)
@pytest.mark.parametrize(
    ("reason", "status"), [(view.OUTDATED, 401), (view.MISMATCH, 503)]
)
async def test_a_claim_atom_honours_the_views_indeterminate(
    gen: Any, atom: Callable[[Any], Any], key: str, reason: str, status: int
) -> None:
    """The view removed the atom's key because the token predates the claim
    policy (one refresh fixes it) or issuer and verifier disagree: the atom
    answers that indeterminate, never a 402/403 for the token's life, and
    its negation never allows."""
    ctx = _authz(_viewed(key, reason))
    decision = await atom(gen).decide(ctx)
    assert decision.indeterminate and decision.http_status == status
    assert decision.code == reason
    negated = await Not(atom(gen)).decide(ctx)
    assert not negated.allowed


@pytest.mark.asyncio
@pytest.mark.parametrize(("atom", "key"), CLAIM_ATOMS, ids=CLAIM_IDS)
async def test_a_definite_untrusted_reason_never_allows(
    gen: Any, atom: Callable[[Any], Any], key: str
) -> None:
    """An unassigned key, or one whose authority was absent at the mint (the
    view's other reasons): for a generated claim atom nobody vouched for the
    claim, 501 ``access_unknown`` (KB-33d); the kernel ``Subscribed`` denies
    definitely, as before. Neither allows."""
    generated = key in GENERATED_KEYS
    for reason in (view.UNASSIGNED, view.SOURCE_ABSENT):
        ctx = _authz(_viewed(key, reason))
        decision = await atom(gen).decide(ctx)
        assert not decision.allowed, reason
        if generated:
            assert _unknown(decision), reason
            assert _unknown(await Not(atom(gen)).decide(ctx)), reason
        else:
            assert not decision.indeterminate, reason


# --------------------------------------------------------------------------- #
# SEC-5: a previous= authority's name is its module's; owners in the marker
# --------------------------------------------------------------------------- #


def _entry_point(name: str, path: str) -> FakeEntryPoint:
    return FakeEntryPoint(name, path, f"dist-{name}")


def test_a_previous_authority_named_unlike_its_installed_module_is_refused() -> None:
    """``sandloom_identity`` is installed as ``identity``: a ``previous=``
    naming it ``billing`` would make the verifier read ``billing``'s
    provenance for a digest identity's mint stamped. Refused at the map."""
    entry = Authority(
        ACME_ID, previous=Authority("billing", module="sandloom_identity")
    )
    with installed_entry_points(), pytest.raises(ClaimAuthorityError, match="billing"):
        build_claim_authorities({"issuer": ACME_ID, "roles": entry})


def test_a_previous_authority_named_unlike_its_app_module_is_refused() -> None:
    """Whatever the path, an installed module has one name: ``acme.idp``
    installed as ``idp`` is never ``grants``."""
    entry = Authority(ACME_ID, previous=Authority("grants", module=IDP))
    with installed_entry_points(_entry_point("idp", IDP)):
        with pytest.raises(ClaimAuthorityError, match="grants"):
            build_claim_authorities({"issuer": ACME_ID, "roles": entry})


def test_a_previous_authority_no_longer_installed_keeps_its_declared_name() -> None:
    """After the rollout the old module may be gone from the image: its
    entry-point name cannot be read back, so the declared name stands."""
    entry = Authority(
        ACME_ID, previous=Authority("identity", module="sandloom_identity")
    )
    with installed_entry_points(_entry_point("tenancy", "sandloom_tenancy")):
        policy = build_claim_authorities({"issuer": ACME_ID, "roles": entry})
    assert dict(policy.previous_owners) == {"roles": "identity"}


def test_the_verifier_marker_covers_the_owners() -> None:
    """The verified-token cache is keyed by the marker: two policies that
    differ only in the short name they accept a key's authority under (now
    or as ``previous=``) must not share cached views."""
    base: dict[str, Any] = {
        "authorities": {"roles": IDP},
        "owners": {"roles": "idp"},
        "issuer": IDP,
        "assigned": True,
        "previous": {"roles": GRANTS},
        "previous_owners": {"roles": "grants"},
    }
    marker = ClaimAuthorities(**base).marker
    assert ClaimAuthorities(**{**base, "owners": {"roles": "other"}}).marker != marker
    renamed = {**base, "previous_owners": {"roles": "other"}}
    assert ClaimAuthorities(**renamed).marker != marker
    assert ClaimAuthorities(**base).marker == marker


# --------------------------------------------------------------------------- #
# The gate probe G2-G4 shape at the mint: the authority ran and omitted the
# key, a non-authority contributor wrote it (ADR-0014 Context)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["roles", "permissions", "tenant_ids"])
async def test_a_key_the_authority_omitted_is_never_a_foreign_writers(key: str) -> None:
    """``acme.idp`` / ``acme.grants`` ran and wrote nothing for ``key``;
    ``acme.rogue`` (optional) wrote it: the rogue loses its whole result,
    the token carries no ``key`` and records the rogue as failed. Critical,
    the rogue refuses the mint."""
    core = _core()
    _register(core, IDP, {}, critical=True)
    _register(core, GRANTS, {}, critical=True)
    _register(core, ROGUE, {key: ["admin"], "loyalty": 1})
    claims = await _mint(core)
    assert key not in claims and "loyalty" not in claims
    assert claims["claims_sources_failed"] == ["rogue"]
    assert {"idp", "grants"} <= set(claims["claims_sources"])
    critical = _core()
    _register(critical, IDP, {}, critical=True)
    _register(critical, GRANTS, {}, critical=True)
    _register(critical, ROGUE, {key: ["admin"]}, critical=True)
    with pytest.raises(ClaimsUnavailable) as refused:
        await _mint(critical)
    assert refused.value.owner == "rogue"
