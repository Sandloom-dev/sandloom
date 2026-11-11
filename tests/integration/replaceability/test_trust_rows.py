"""Trust rows T-1..T-8 (ADR-0014 fitness tests, KB-10): claim authorities
assigned by the composition root, the mint check and the violator rule, on
the replaceability fixtures, mono and split.

From ``design/kernel-boundary/poc/trust/run.py``. Identity, tenancy and billing
are not installed (the ``kernel-without-batteries`` job runs this too): the
issuer is ``test_identity``, the tenant-grant provider ``grants``, the violator
``forged``. Each topology boots a fixture composition root
(``pocapp/roots``): the map is code, never a setting.

The platform gates (``require_platform_admin``, ``/api/v1/platform/...``) still
trust the module NAME ``identity`` (B-S1, B-S2): they open for a replacement
issuer only with KB-11's one platform admission, so T-1 asserts them closed
here, and the read-side halves of the rows (a token minted elsewhere) are
KB-11's.
"""

from __future__ import annotations

import re
import time

import httpx
import pytest
from test_replaceability import (
    ALL,
    IDENTITY_BATTERY,
    NO_GRANTS_ROOT,
    NOT_AUTHORIZED,
    SPLIT_BINDINGS,
    T_A,
    T_FOREIGN,
    UNAVAILABLE_FORGED,
    UNAVAILABLE_GRANTS,
    UNAVAILABLE_ISSUER,
    Infra,
    M,
    Proc,
    Users,
    boot_refusal,
    forge,
    gate_statuses,
    get,
    grant,
    mint,
    mono,
    running,
    split,
    token_of,
)

pytestmark = [pytest.mark.integration, pytest.mark.e2e, pytest.mark.slow]

ISSUER_ONLY_ROOT = "pocapp.roots.issuer_only:app"
TYPO_ROOT = "pocapp.roots.typo:app"
LEAN_ROOT = "sandloom.main:app"

#: ``forged`` writing every protected or reserved key it could abuse.
FORGERY = {
    "roles": ["admin"],
    "permissions": ["perm.poc.write"],
    "tenant_ids": [T_FOREIGN],
    "claims_sources": ["test_identity"],
    "cpv": "1:forged",
    "tenant_id": T_FOREIGN,
}
_VIOLATIONS = re.compile(
    r'^sandloom_facade_counter_total\{name="claims_contributor_violations_total"\} '
    r"([0-9.]+)$",
    re.MULTILINE,
)


def violations(proc: Proc) -> float:
    """``claims_contributor_violations_total`` as ``proc`` exports it."""
    response = httpx.get(
        proc.url + "/metrics",
        headers={"X-Internal-Token": proc.env["INTERNAL_API_TOKEN"]},
        timeout=20,
    )
    assert response.status_code == 200, response.text[:500]
    found = _VIOLATIONS.search(response.text)
    return float(found.group(1)) if found else 0.0


def _topology(name: str, infra: Infra, users: Users, **extra: str) -> list[Proc]:
    if name == "mono":
        return mono(infra, users, ALL, **extra)
    return split(infra, users, SPLIT_BINDINGS, **extra)


# --------------------------------------------------------------------------- #
# T-1: a replacement issuer, assigned
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("topology", ["mono", "split"])
def test_t1_the_assigned_replacement_issuer_grants_rights(
    infra: Infra, topology: str
) -> None:
    """T-1: ``Role``, ``Perm``, ``TenantScope`` and RLS from the assigned
    authorities; identity is not installed and no kernel file names them."""
    users = Users()
    with running(infra, _topology(topology, infra, users)) as procs:
        issuer, provider = procs[0], procs[-1]
        forge(provider, {})
        assert grant(provider, users.admin, T_A).status == 200
        admin = mint(issuer, users.admin)
        assert admin.body["claims"]["roles"] == ["admin"]
        assert admin.body["claims"]["tenant_ids"] == [T_A]
        token = token_of(admin)
        assert gate_statuses(issuer, token) == {
            "/api/v1/shop/role-admin": 200,
            "/api/v1/shop/perm-write": 200,
            # one platform admission over the trusted view (KB-11, B-S1 and
            # B-S2 fixed): no source is trusted by the name "identity"
            "/api/v1/shop/platform": 200,
            "/api/v1/platform/shop/services": 200,
        }
        tenant = get(issuer, "/api/v1/shop/tenant", token, tenant=T_A)
        assert (tenant.status, tenant.body) == (200, {"tenant": T_A})
        notes = get(issuer, "/api/v1/notes/items", token, tenant=T_A)
        assert notes.body["bodies"] == ["note of A"]
        user = token_of(mint(issuer, users.user))
        assert set(gate_statuses(issuer, user).values()) == {403}


# --------------------------------------------------------------------------- #
# T-2, T-4: an optional violator
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("topology", ["mono", "split"])
def test_t2_an_optional_violator_loses_its_whole_result(
    infra: Infra, topology: str
) -> None:
    """T-2: the authorities' values only, ``forged`` in failed, ERROR, +1."""
    users = Users()
    with running(infra, _topology(topology, infra, users)) as procs:
        issuer, provider = procs[0], procs[-1]
        assert grant(provider, users.user, T_A).status == 200
        forge(provider, {**FORGERY, "loyalty_tier": "gold"})
        before = violations(issuer)
        answer = mint(issuer, users.user)
        assert answer.status == 200, answer
        claims = answer.body["claims"]
        assert claims["roles"] == []
        assert claims["permissions"] == []
        assert claims["tenant_ids"] == [T_A]
        assert claims["claims_sources_failed"] == ["forged"]
        assert claims["claims_sources"] == ["grants", "test_identity"]
        for key in ("loyalty_tier", "tenant_id"):
            assert key not in claims, key
        # the kernel's own per-key policy version, never the forged one
        assert claims["cpv"]["v"] == 1 and claims["cpv"] != FORGERY["cpv"]
        token = token_of(answer)
        assert set(gate_statuses(issuer, token).values()) == {403}
        foreign = get(issuer, "/api/v1/shop/tenant", token, tenant=T_FOREIGN)
        assert (foreign.status, foreign.detail) == (403, NOT_AUTHORIZED)
        assert violations(issuer) == before + 1
        assert "claims contributor 'forged' wrote the claim" in issuer.log_text()
        assert "its whole contribution is dropped" in issuer.log_text()
        # writing only its own key, it contributes normally
        forge(provider, {"loyalty_tier": "gold"})
        own = mint(issuer, users.user).body["claims"]
        assert own["loyalty_tier"] == "gold"
        assert own["claims_sources"] == ["forged", "grants", "test_identity"]


@pytest.mark.parametrize("topology", ["mono", "split"])
def test_t4_a_hundred_logins_with_a_violator_all_succeed(
    infra: Infra, topology: str
) -> None:
    """T-4: 100 tokens, 0 x 503, 0 with ``admin``, the counter at 100."""
    users = Users()
    with running(infra, _topology(topology, infra, users)) as procs:
        issuer, provider = procs[0], procs[-1]
        forge(provider, {"roles": ["admin"], "jti": "forged"})
        before = violations(issuer)
        answers = [mint(issuer, users.user) for _ in range(100)]
        assert {a.status for a in answers} == {200}
        assert sum("admin" in a.body["claims"]["roles"] for a in answers) == 0
        assert violations(issuer) == before + 100
        assert issuer.log_text().count("claims contributor 'forged' wrote") == 100


# --------------------------------------------------------------------------- #
# T-3: a critical violator
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("first", [True, False], ids=["forged-first", "forged-last"])
def test_t3_a_critical_violator_refuses_the_mint_naming_itself(
    infra: Infra, first: bool
) -> None:
    """T-3 mono: 503 naming ``forged``, never the authority, in either order."""
    users = Users()
    rest = [m for m in ALL if m != f"{M}forged"]
    order = [f"{M}forged", *rest] if first else [*rest, f"{M}forged"]
    with running(infra, mono(infra, users, order, POC_FORGED_CRITICAL="1")) as (p,):
        forge(p, {"roles": ["admin"]})
        answer = mint(p, users.user)
        assert (answer.status, answer.detail) == (503, UNAVAILABLE_FORGED)
        forge(p, {"loyalty_tier": "gold"})
        assert mint(p, users.user).status == 200


def test_t3_a_critical_remote_violator_refuses_the_mint(infra: Infra) -> None:
    """T-3 split: the ``forged.claims.v1`` binding is critical."""
    users = Users()
    bindings = {**SPLIT_BINDINGS, "forged.claims.v1": {}}
    with running(infra, split(infra, users, bindings)) as (p1, p2):
        forge(p2, {"roles": ["admin"]})
        answer = mint(p1, users.user)
        assert (answer.status, answer.detail) == (503, UNAVAILABLE_FORGED)
        forge(p2, {"loyalty_tier": "gold"})
        assert mint(p1, users.user).status == 200


# --------------------------------------------------------------------------- #
# T-5: startup refusals
# --------------------------------------------------------------------------- #


def test_t5_a_typo_in_an_authority_refuses_the_boot(infra: Infra) -> None:
    """T-5: the assigned path names no module: refused, named as a typo."""
    log = boot_refusal(infra, mono(infra, Users(), ALL, root=TYPO_ROOT))
    assert "SANDLOOM-B035" in log
    assert (
        "claim 'roles' is assigned to 'pocapp.modules.test_identty', which is "
        "neither loaded in this issuing process nor bound" in log
    )
    assert "a typo of 'pocapp.modules.test_identity'?" in log


def test_t5_an_authority_neither_loaded_nor_bound_refuses_the_boot(
    infra: Infra,
) -> None:
    """T-5: ``grants`` is assigned ``tenant_ids`` but not composed."""
    procs = mono(infra, Users(), [f"{M}test_identity", f"{M}shop"])
    log = boot_refusal(infra, procs)
    assert "SANDLOOM-B035" in log
    assert (
        "claim 'tenant_ids' is assigned to 'pocapp.modules.grants', which is neither "
        "loaded in this issuing process nor bound" in log
    )


def test_t5_a_non_critical_authority_refuses_the_boot(infra: Infra) -> None:
    """T-5 split: ``grants.claims.v1`` bound ``critical: false``."""
    bindings = {**SPLIT_BINDINGS, "grants.claims.v1": {"critical": False}}
    p1, p2 = split(infra, Users(), bindings)
    p2.close()
    log = boot_refusal(infra, [p1])
    assert "SANDLOOM-B035" in log
    assert "whose binding grants.claims.v1 is critical: false" in log


# --------------------------------------------------------------------------- #
# T-6: nothing assigned
# --------------------------------------------------------------------------- #


def test_t6_without_an_assignment_no_writer_is_trusted(infra: Infra) -> None:
    """T-6: ``roles``/``tenant_ids`` unassigned; the violator's writes are
    dropped and the login still mints a token without them. The issuer's
    contributor is critical, as decision 7 requires of every issuer (an
    optional one refuses the boot, below), and writes no unassigned key
    here; an issuer that does refuses every mint (the next row)."""
    users = Users()
    modules = [f"{M}test_identity", f"{M}forged", f"{M}notes", f"{M}shop"]
    procs = mono(
        infra, users, modules, root=ISSUER_ONLY_ROOT, POC_TEST_IDENTITY_SILENT="1"
    )
    with running(infra, procs) as (proc,):
        forge(proc, {"roles": ["admin"], "tenant_ids": [T_A]})
        answer = mint(proc, users.admin)
        assert answer.status == 200, answer
        claims = answer.body["claims"]
        assert "roles" not in claims
        assert "tenant_ids" not in claims
        assert claims["claims_sources_failed"] == ["forged"]
        token = token_of(answer)
        assert set(gate_statuses(proc, token).values()) == {403}
        none = get(proc, "/api/v1/shop/tenant", token)
        assert (none.status, none.detail) == (400, "tenant context required")
        for path in ("/api/v1/shop/tenant", "/api/v1/notes/items"):
            seen = get(proc, path, token, tenant=T_A)
            assert (seen.status, seen.detail) == (403, NOT_AUTHORIZED), path


def test_t6_a_critical_issuer_without_an_assignment_refuses_every_mint(
    infra: Infra,
) -> None:
    """T-6: fail closed, never a token with untrusted rights (ADR-0012 d. 3)."""
    users = Users()
    modules = [f"{M}test_identity", f"{M}shop"]
    procs = mono(infra, users, modules, root=ISSUER_ONLY_ROOT)
    with running(infra, procs) as (proc,):
        answer = mint(proc, users.admin)
        assert (answer.status, answer.detail) == (503, UNAVAILABLE_ISSUER)


def test_t6_an_issuer_under_a_root_without_a_map_refuses_the_boot(
    infra: Infra,
) -> None:
    """T-6, no map at all (the lean root with an issuer): the boot is refused
    (SANDLOOM-B035) rather than an app whose every login is refused (503)."""
    modules = [f"{M}test_identity", f"{M}shop"]
    log = boot_refusal(infra, mono(infra, Users(), modules, root=LEAN_ROOT))
    assert "SANDLOOM-B035" in log
    assert "no claim_authorities map is set" in log
    assert "'pocapp.modules.test_identity'" in log
    assert "the root that composes 'pocapp.modules.test_identity' must pass" in log
    # No installed root's map names this issuer: none is suggested.
    assert "sandloom_standard.main" not in log


def test_t6_an_issuer_with_an_optional_contributor_refuses_the_boot(
    infra: Infra,
) -> None:
    """T-6, decision 7: the issuer fields have no fallback, so an issuer
    whose contributor is optional refuses the boot instead of minting
    tokens without them (every invitation accept would fail closed)."""
    modules = [f"{M}test_identity", f"{M}shop"]
    procs = mono(
        infra,
        Users(),
        modules,
        root=ISSUER_ONLY_ROOT,
        POC_TEST_IDENTITY_CRITICAL="0",
    )
    log = boot_refusal(infra, procs)
    assert "SANDLOOM-B035" in log
    assert (
        "the claim_authorities issuer 'pocapp.modules.test_identity' registers "
        "its claims contributor critical=False"
    ) in log


# --------------------------------------------------------------------------- #
# T-7, T-8: spoofing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("critical", ["0", "1"], ids=["optional", "critical"])
def test_t7_a_module_named_identity_gets_no_rights(infra: Infra, critical: str) -> None:
    """T-7: an app-local ``identity`` granting admin to everyone is a violator."""
    users = Users()
    procs = mono(
        infra,
        users,
        [*ALL, f"{M}identity"],
        POC_IDENTITY_ISSUER="0",
        POC_IDENTITY_CRITICAL=critical,
        POC_IDENTITY_ADMIN_ALL="1",
    )
    if IDENTITY_BATTERY:
        assert "SANDLOOM-B020" in boot_refusal(infra, procs)
        return
    with running(infra, procs) as (proc,):
        forge(proc, {})
        answer = mint(proc, users.user)
        if critical == "1":
            assert answer.status == 503
            assert answer.detail == (
                "Token claims from 'identity' are unavailable; try again later"
            )
            return
        assert answer.status == 200, answer
        assert answer.body["claims"]["roles"] == []
        assert answer.body["claims"]["claims_sources_failed"] == ["identity"]
        assert set(gate_statuses(proc, token_of(answer)).values()) == {403}


def test_t8_another_modules_claims_contract_refuses_the_boot(infra: Infra) -> None:
    """T-8 (a): ``forged`` registering ``grants.claims.v1`` where ``grants``
    is not loaded, to answer for it (B034; with ``grants`` loaded the
    registration is a duplicate and refused anyway)."""
    modules = [f"{M}test_identity", f"{M}forged", f"{M}shop"]
    procs = mono(
        infra, Users(), modules, root=NO_GRANTS_ROOT, POC_FORGED_SQUAT="grants"
    )
    log = boot_refusal(infra, procs)
    assert "SANDLOOM-B034" in log
    assert "registers contract 'grants.claims.v1' under module 'grants'" in log


def test_t8_a_binding_to_a_process_without_the_authority_fails_the_mint(
    infra: Infra,
) -> None:
    """T-8 (b): P1 binds ``grants.claims.v1`` to P2, which does not host
    ``grants``: the critical binding fails, the mint is 503 naming it. P1's
    ``/ready`` fails too: the binding is held to the kernel's schema of
    ``<m>.claims.v1`` and P2 does not serve it (KB-21)."""
    users = Users()
    p1, p2 = split(infra, users, SPLIT_BINDINGS)
    p2.modules = [m for m in p2.modules if m != f"{M}grants"]
    with running(infra, [p2]):
        try:
            p1.start(infra)
            assert "grants.claims.v1: not served by the provider" in _not_ready(p1)
            answer = mint(p1, users.user)
            assert (answer.status, answer.detail) == (503, UNAVAILABLE_GRANTS)
        finally:
            p1.close()


def _not_ready(proc: Proc, timeout: float = 90.0) -> str:
    """The ``/ready`` body of ``proc`` once it serves and answers 503."""
    deadline = time.monotonic() + timeout
    headers = {"X-Internal-Token": proc.env["INTERNAL_API_TOKEN"]}
    while time.monotonic() < deadline:
        try:
            ready = httpx.get(proc.url + "/ready", headers=headers, timeout=2)
            if ready.status_code == 503 and "remote_services" in ready.text:
                return ready.text
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    pytest.fail(f"{proc.label} never failed /ready:\n{proc.log_text()[-4000:]}")


def test_the_no_grants_root_mints_without_tenant_claims(infra: Infra) -> None:
    """The fixture's map without a grant provider assigns no ``tenant_ids``."""
    users = Users()
    modules = [f"{M}test_identity", f"{M}forged", f"{M}shop"]
    with running(infra, mono(infra, users, modules, root=NO_GRANTS_ROOT)) as (proc,):
        forge(proc, {})
        claims = mint(proc, users.user).body["claims"]
        assert "tenant_ids" not in claims
        assert claims["claims_sources_failed"] == []
