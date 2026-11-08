"""Claim authorities: the mint check, the violator rule and the startup
refusals (ADR-0014 decisions 1, 2 and 7; SANDLOOM-B035).

Kernel-only: the modules are fake import paths (``acme.*``) registered on a
facade with fake databases, so no battery is needed and no I/O happens. The
running-process rows (T-1..T-8, mono and split) are in
``tests/integration/replaceability/test_trust_rows.py``.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

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
from sandloom.core.claims import (
    FAIL_SAFE_KEYS,
    NO_AUTHORITIES,
    PROTECTED_BASE,
    ClaimAuthorities,
)
from sandloom.core.config.common import ConfigurationError
from sandloom.core.facade import ClaimsUnavailable, CoreFacade
from sandloom.core.facade._claims import CLAIMS_VIOLATIONS_METRIC, RESERVED_CLAIM_KEYS

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
    core.assign_claim_authorities(build_claim_authorities(mapping))
    return core


def _register(
    core: CoreFacade, path: str, result: dict[str, Any], *, critical: bool = False
) -> _Counted:
    fn = _Counted(result)
    with core.module_registration(path) as api:
        api.add_claims_contributor(fn, critical=critical)
    return fn


async def _mint(core: CoreFacade) -> dict[str, Any]:
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
    assert policy.owner_of("email") is None
    assert policy.assigned
    assert policy.protected == PROTECTED_BASE | (set(MAP) - {"issuer"})


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
@pytest.mark.parametrize("first", [True, False], ids=["violator-first", "violator-last"])
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
@pytest.mark.parametrize("first", [True, False], ids=["violator-first", "violator-last"])
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
        "grants.claims.v1": _Proxy({"tenant_ids": ["t"]}),
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
    proxy = _Proxy({"tenant_ids": []})
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


@pytest.fixture
def known_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``acme.*`` paths name modules; anything else is a typo."""
    monkeypatch.setattr(
        _ca, "_names_a_module", lambda path: path.startswith(("acme.", "other."))
    )


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


@pytest.mark.usefixtures("known_modules")
def test_a_composition_that_honours_the_map_passes() -> None:
    """Local authorities, critical where required; the plans one non-critical."""
    assert _problems(MAP) == []


@pytest.mark.usefixtures("known_modules")
def test_startup_refuses_a_typo() -> None:
    """T-5: an assigned path that names no module."""
    (problem,) = _problems({**MAP, "roles": "acme_idp"})
    assert "claim_authorities['roles'] is 'acme_idp', which names no module" in problem


def test_the_typo_check_reads_the_real_import_system() -> None:
    """Without the fixture: a real module path passes, a missing one does not."""
    assert _ca._names_a_module("sandloom.core.claims")
    assert not _ca._names_a_module("sandloom.core.no_such_module_kb10")


@pytest.mark.usefixtures("known_modules")
def test_startup_refuses_an_authority_neither_loaded_nor_bound() -> None:
    """T-5: the issuing process cannot reach the authority."""
    (problem,) = _problems(MAP, loaded={"idp": IDP, "plans": PLANS})
    assert "claim 'tenant_ids' is assigned to 'acme.grants', which is neither" in problem
    assert "grants.claims.v1 is not in SERVICE_BINDINGS" in problem


@pytest.mark.usefixtures("known_modules")
def test_startup_refuses_a_non_critical_authority_of_a_rights_key() -> None:
    """T-5 / decision 7: ``roles`` from a critical=False contributor."""
    problems = _problems(MAP, contributors={**CONTRIBUTORS, "idp": False})
    assert len(problems) == 2  # roles and permissions
    assert all("critical=False" in p for p in problems)


@pytest.mark.usefixtures("known_modules")
def test_startup_refuses_a_non_critical_binding_of_a_rights_key() -> None:
    """Split: ``grants.claims.v1`` with ``critical: false`` writing tenant_ids."""
    (problem,) = _problems(
        MAP,
        loaded={"idp": IDP, "plans": PLANS},
        bindings={"grants.claims.v1": {"base_url": "http://g", "critical": False}},
    )
    assert "whose binding grants.claims.v1 is critical: false" in problem


@pytest.mark.usefixtures("known_modules")
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


@pytest.mark.usefixtures("known_modules")
def test_startup_refuses_an_authority_without_a_contributor() -> None:
    """A loaded authority that writes nothing cannot vouch for its key."""
    (problem,) = _problems(MAP, contributors={"idp": True, "plans": False})
    assert "'acme.grants'" in problem
    assert "registers no claims contributor" in problem


@pytest.mark.usefixtures("known_modules")
def test_startup_refuses_a_module_of_the_same_name_from_another_path() -> None:
    """T-7: the short name is not proof of the authority."""
    (problem,) = _problems(MAP, loaded={**LOADED, "grants": "other.grants"})
    assert "the module loaded here under the name 'grants' is 'other.grants'" in problem


@pytest.mark.usefixtures("known_modules")
def test_startup_refuses_an_issuer_that_is_not_the_maps() -> None:
    """X2: an ``issues_tokens`` module that is not the map's issuer."""
    problems = _problems({**MAP, "issuer": GRANTS}, issuers=(IDP,))
    assert any("module 'acme.idp' declares issues_tokens=True" in p for p in problems)
    assert any(
        "issuer 'acme.grants' is loaded here but does not declare" in p
        for p in problems
    )


@pytest.mark.usefixtures("known_modules")
def test_startup_refuses_the_maps_issuer_without_issues_tokens() -> None:
    """X2, the reverse: the map's issuer loaded but not issuing."""
    assert _problems(MAP, issuers=()) == [
        "the claim_authorities issuer 'acme.idp' is loaded here but does not "
        "declare issues_tokens=True"
    ]


@pytest.mark.usefixtures("known_modules")
def test_a_map_without_issuer_refuses_a_loaded_issuer() -> None:
    """An assigned map with no ``issuer`` entry and an issuing module."""
    mapping = {k: v for k, v in MAP.items() if k != "issuer"}
    (problem,) = _problems(mapping)
    assert "but the claim_authorities issuer is None" in problem


@pytest.mark.usefixtures("known_modules")
def test_a_non_issuing_process_checks_only_the_issuer() -> None:
    """A split's provider process neither mints nor needs the authorities."""
    assert _problems(MAP, loaded={"plans": PLANS}, issuers=()) == []


def test_no_map_has_no_refusal() -> None:
    """A lean root refuses nothing at startup (the mint refuses instead)."""
    assert (
        claim_authority_problems(
            NO_AUTHORITIES, loaded=LOADED, contributors={}, bindings={}, issuers=[IDP]
        )
        == []
    )


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


@pytest.mark.usefixtures("known_modules")
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


@pytest.mark.usefixtures("known_modules")
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
    assert "'nowhere', which names no module" in message
    assert "critical=False" in message
    assert not core.claim_authorities.assigned


def test_install_without_a_map_warns_in_an_issuing_process(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ADR-0012 decision 3: startup logs that no authority map is set."""
    core = _core(None)
    with caplog.at_level(logging.WARNING, logger="test"):
        policy = install_claim_authorities(
            core,
            [_descriptor(IDP, issues_tokens=True)],
            claim_authorities=None,
            default_claim_authorities=None,
            bindings={},
            log=logging.getLogger("test"),
        )
    assert policy is NO_AUTHORITIES
    assert any(
        "no claim_authorities map is set" in r.getMessage() for r in caplog.records
    )
