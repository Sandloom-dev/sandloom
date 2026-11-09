"""``Subscribed()`` and ``Subscribed(statuses=...)`` agree (KB-33g).

Owner decision 2026-11-09: ``statuses=`` changes the accepted statuses, not
the trust rules. Billing's contract: ``subscription_claims()`` returns ``{}``
for a user with no billing customer, ``{"subscription_status": "none"}`` for
a customer without a subscription. So billing's own claim proven absent (the
key assigned to billing and verified, billing ran OK at the mint, the token's
claim policy intact, the view removed nothing) is billing's definite 402 on
both paths; every other source of absence stays indeterminate:

==========================================================  ===================
row                                                         both paths
==========================================================  ===================
billing verified, ran OK, policy intact, status absent      402 deny
status present, not accepted                                402 deny
billing failed at the mint                                  503
removed by the view: token predates the policy              401
removed by the view: issuer / verifier policy mismatch      503
no authority assigned for the key                           501
no provable provenance of a successful billing run          501
wrong type                                                  501
==========================================================  ===================

``~Subscribed`` allows only on a proven deny; ``Not`` / ``Or`` / ``And``
follow the same rows; ``Account.user()`` asks no contract, looks no fact up
(beyond the owner's local rule for a present status on the default path),
resolves nothing live and touches no database.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI

import sandloom.shared.authz.facts as facts
from sandloom.core.app_factory.claim_authorities import build_claim_authorities
from sandloom.core.auth import claims_view as view
from sandloom.core.claims import CPV_CLAIM, CPV_VERSION, cpv_digest
from sandloom.core.cli.generated import render_stub
from sandloom.core.facade import CoreFacade
from sandloom.shared.authz import Account, Not, Or, Role
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.authz.contract_check import ATOM_ATTR, ClaimFactCheck
from sandloom.shared.authz.facts import AccessFact, ClaimRule
from sandloom.shared.authz.results import Category
from sandloom.shared.subject import UserContext
from sandloom_billing.access import SUBSCRIPTION_ACTIVE_FACT
from sandloom_billing.contracts import EntitlementsContract

pytestmark = pytest.mark.asyncio

KEY = "subscription_status"
BILLING_PATH = "sandloom_billing"
MAP = {
    "issuer": "sandloom_identity",
    "roles": "sandloom_identity",
    "permissions": "sandloom_identity",
    KEY: BILLING_PATH,
}
UNASSIGNED_MAP = {k: v for k, v in MAP.items() if k != KEY}
#: The key assigned to another authority (a module that is not billing).
OTHER_MAP = {**MAP, KEY: "sandloom_identity"}
#: The cpv the issuer of MAP stamps (every assigned key, written or not).
DIGESTS: dict[str, str] = dict(build_claim_authorities(MAP).digests)
CPV: dict[str, Any] = {"v": CPV_VERSION, "k": DIGESTS}
#: Another issuer's policy for the key (an authority this verifier does not
#: accept for billing).
CPV_OTHER = {
    "v": CPV_VERSION,
    "k": {**DIGESTS, KEY: cpv_digest(KEY, "sandloom_identity")},
}
ALLOW = (True, None)
DENY = (False, None)
STATUSES = frozenset({"active", "trialing"})
ENTITLEMENTS = "billing.entitlements.v1"
_ABSENT = object()


def _generated(tmp_path: Path) -> ModuleType:
    """Billing's stub as ``sandloom generate`` writes it for a consumer."""
    path = tmp_path / "billing_entitlements_v1.py"
    path.write_text(render_stub(ENTITLEMENTS, EntitlementsContract))
    spec = importlib.util.spec_from_file_location("_kb33g_billing_stub", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gen(tmp_path: Path) -> Any:
    module = _generated(tmp_path)
    yield module
    sys.modules.pop(module.__name__, None)


class _NoIO:
    """A composition with billing's fact registered locally (a registry
    lookup, not a call) whose contracts may not be asked."""

    def __init__(self, mapping: dict[str, str] | None, *, local: bool = True) -> None:
        self.core = CoreFacade(app=FastAPI())
        if mapping is not None:
            self.core._assign_claim_authorities(build_claim_authorities(mapping))
        self.local = local
        self.asked: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.core, name)

    def get_access_fact(self, name: str) -> Any:
        if self.local and name == SUBSCRIPTION_ACTIVE_FACT.name:
            return SUBSCRIPTION_ACTIVE_FACT
        return None

    def remote_fact_owners(self) -> list[str]:
        return [] if self.local else ["billing"]

    def get_service(self, name: str) -> Any:
        self.asked.append(name)
        raise AssertionError(f"Account.user() asked the contract {name!r}")


class _NoDB:
    """A request session nobody may touch (zero SQL)."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"Account.user() used the database ({name})")


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count fact lookups and live resolutions (lookups delegate: a local
    registry read; a live resolution fails the test)."""
    calls = {"lookup": 0, "resolve": 0}
    lookup = facts.lookup_access_fact

    async def _lookup(*args: Any, **kwargs: Any) -> Any:
        calls["lookup"] += 1
        return await lookup(*args, **kwargs)

    async def _resolve(*args: Any, **kwargs: Any) -> Any:
        calls["resolve"] += 1
        raise AssertionError("Account.user() resolved a fact live")

    monkeypatch.setattr(facts, "lookup_access_fact", _lookup)
    monkeypatch.setattr(facts, "resolve_fact", _resolve)
    return calls


def _subject(
    *,
    failed: set[str] | None = None,
    sources: set[str] | None = None,
    untrusted: str | None = None,
    status: Any = _ABSENT,
    cpv: Any = CPV,
) -> UserContext:
    """A caller with roles {member}. ``failed`` the sources that failed at
    the mint (``None``: a legacy token without provenance), ``sources`` the
    ones that ran (default: identity and billing, less the failed),
    ``status`` its ``subscription_status``, ``cpv`` the claim policy the
    issuer stamped (``None``: none)."""
    data: dict[str, Any] = {"user_id": uuid4(), "roles": {"member"}}
    if status is not _ABSENT:
        data[KEY] = status
    if failed is not None:
        ran = {"identity", "billing"} if sources is None else sources
        data["claims_sources"] = ran - failed
        data["claims_sources_failed"] = set(failed)
    if cpv is not None:
        data[CPV_CLAIM] = cpv
    if untrusted is not None:
        data[view.UNTRUSTED_KEY] = {KEY: untrusted}
    return UserContext(**data)


#: row -> (subject, claim authorities map, outcome on BOTH paths). An outcome
#: is ALLOW / DENY or (status, code).
TABLE: dict[str, tuple[Any, dict[str, str] | None, Any]] = {
    # billing is the verified authority, ran OK at the mint, status absent
    "absent.proven": (lambda: _subject(failed=set()), MAP, DENY),
    "absent.proven.other_source_failed": (
        lambda: _subject(failed={"rogue"}),
        MAP,
        DENY,
    ),
    # status present
    "present.active": (lambda: _subject(failed=set(), status="active"), MAP, ALLOW),
    "present.trialing": (
        lambda: _subject(failed=set(), status="trialing"),
        MAP,
        ALLOW,
    ),
    "present.canceled": (
        lambda: _subject(failed=set(), status="canceled"),
        MAP,
        DENY,
    ),
    "present.none": (lambda: _subject(failed=set(), status="none"), MAP, DENY),
    "present.legacy": (lambda: _subject(status="active", cpv=None), MAP, ALLOW),
    # billing failed at the mint
    "failed": (
        lambda: _subject(failed={"billing"}),
        MAP,
        (503, "claim_authority_failed"),
    ),
    "failed.no_map": (
        lambda: _subject(failed={"billing"}),
        None,
        (503, "claim_authority_failed"),
    ),
    "failed.view_source_absent": (
        lambda: _subject(failed={"billing"}, untrusted=view.SOURCE_ABSENT),
        MAP,
        (503, "claim_authority_failed"),
    ),
    "failed.present_unremoved": (
        lambda: _subject(failed={"billing"}, status="active"),
        None,
        (503, "claim_authority_failed"),
    ),
    # removed by the trusted view
    "view.outdated": (
        lambda: _subject(failed=set(), untrusted=view.OUTDATED),
        MAP,
        (401, view.OUTDATED),
    ),
    "view.mismatch": (
        lambda: _subject(failed=set(), untrusted=view.MISMATCH),
        MAP,
        (503, view.MISMATCH),
    ),
    "view.outdated.failed": (
        lambda: _subject(failed={"billing"}, untrusted=view.OUTDATED),
        MAP,
        (401, view.OUTDATED),
    ),
    "view.outdated.legacy": (
        lambda: _subject(untrusted=view.OUTDATED, cpv=None),
        MAP,
        (401, view.OUTDATED),
    ),
    "view.unassigned": (
        lambda: _subject(failed=set(), untrusted=view.UNASSIGNED),
        UNASSIGNED_MAP,
        (501, "access_unknown"),
    ),
    "view.source_absent": (
        lambda: _subject(failed=set(), untrusted=view.SOURCE_ABSENT),
        MAP,
        (501, "access_unknown"),
    ),
    # no authority assigned for the key
    "unassigned.key": (
        lambda: _subject(failed=set()),
        UNASSIGNED_MAP,
        (501, "access_unknown"),
    ),
    "unassigned.no_map": (
        lambda: _subject(failed=set()),
        None,
        (501, "access_unknown"),
    ),
    "unassigned.failed_elsewhere": (
        lambda: _subject(failed={"billing"}),
        UNASSIGNED_MAP,
        (501, "access_unknown"),
    ),
    # no provable provenance of a successful billing run
    "unproven.another_authority": (
        lambda: _subject(failed=set()),
        OTHER_MAP,
        (501, "access_unknown"),
    ),
    "unproven.billing_did_not_run": (
        lambda: _subject(failed=set(), sources={"identity"}),
        MAP,
        (501, "access_unknown"),
    ),
    "unproven.legacy_token": (lambda: _subject(cpv=None), MAP, (501, "access_unknown")),
    "unproven.legacy_with_cpv": (lambda: _subject(), MAP, (501, "access_unknown")),
    "unproven.no_cpv": (
        lambda: _subject(failed=set(), cpv=None),
        MAP,
        (501, "access_unknown"),
    ),
    "unproven.cpv_other_policy": (
        lambda: _subject(failed=set(), cpv=CPV_OTHER),
        MAP,
        (501, "access_unknown"),
    ),
    "unproven.cpv_without_key": (
        lambda: _subject(
            failed=set(),
            cpv={
                "v": CPV_VERSION,
                "k": {k: v for k, v in DIGESTS.items() if k != KEY},
            },
        ),
        MAP,
        (501, "access_unknown"),
    ),
    "unproven.cpv_other_version": (
        lambda: _subject(failed=set(), cpv={**CPV, "v": CPV_VERSION + 1}),
        MAP,
        (501, "access_unknown"),
    ),
    "unproven.cpv_malformed": (
        lambda: _subject(failed=set(), cpv="v1"),
        MAP,
        (501, "access_unknown"),
    ),
    "unproven.present_billing_did_not_run": (
        lambda: _subject(failed=set(), sources={"identity"}, status="active"),
        None,
        (501, "access_unknown"),
    ),
    # wrong type
    "malformed.int": (
        lambda: _subject(failed=set(), status=1),
        MAP,
        (501, "access_unknown"),
    ),
    "malformed.bool": (
        lambda: _subject(failed=set(), status=True),
        MAP,
        (501, "access_unknown"),
    ),
    "malformed.list": (
        lambda: _subject(failed=set(), status=["active"]),
        MAP,
        (501, "access_unknown"),
    ),
    "malformed.empty_dict": (
        lambda: _subject(failed=set(), status={}),
        MAP,
        (501, "access_unknown"),
    ),
}

_SWAP = {ALLOW: DENY, DENY: ALLOW}
#: How a wrap maps the atom's outcome: ``Not`` swaps a definite answer and
#: keeps an indeterminate; an allowing sibling of ``Or`` allows on every row.
WRAPS: dict[str, tuple[Any, Any]] = {
    "bare": (lambda a: a, lambda o: o),
    "not": (Not, lambda o: _SWAP.get(o, o)),
    "not_not": (lambda a: Not(Not(a)), lambda o: o),
    "or_denied": (lambda a: Or(Role("ops"), a), lambda o: o),
    "or_not_denied": (lambda a: Or(Role("ops"), Not(a)), lambda o: _SWAP.get(o, o)),
    "and_allowed": (lambda a: Role("member") & a, lambda o: o),
    "and_not": (lambda a: Role("member") & Not(a), lambda o: _SWAP.get(o, o)),
    "not_and": (lambda a: Not(Role("member") & a), lambda o: _SWAP.get(o, o)),
    "or_allowed": (lambda a: Or(Role("member"), a), lambda _o: ALLOW),
}
PATHS: dict[str, Any] = {
    "default": lambda gen: gen.Subscribed(of=Account.user()),
    "statuses": lambda gen: gen.Subscribed(STATUSES, of=Account.user()),
}


def _assert_outcome(decision: Any, expected: Any, *, bare: bool) -> None:
    if expected == ALLOW:
        assert decision.allowed and not decision.indeterminate, decision
    elif expected == DENY:
        assert not decision.allowed and not decision.indeterminate, decision
        if bare:
            assert (decision.http_status, decision.code) == (402, "Subscribed")
    else:
        status, code = expected
        assert not decision.allowed and decision.indeterminate, decision
        assert (decision.http_status, decision.code) == (status, code), decision


@pytest.mark.parametrize("path", sorted(PATHS))
@pytest.mark.parametrize("wrap", sorted(WRAPS))
@pytest.mark.parametrize("row", sorted(TABLE))
async def test_both_paths_answer_every_row_alike(
    gen: Any, counted: dict[str, int], row: str, wrap: str, path: str
) -> None:
    """Every row, both reads, bare and under Not / Or / And: the same
    outcome; no indeterminate is ever an allow, ``Not`` allows only on a
    proven deny; nothing is asked (no contract, no live fact, no SQL), and
    only the default read of a vouched present status reads the owner's
    rule from the local registry."""
    subject, mapping, outcome = TABLE[row]
    build, expect = WRAPS[wrap]
    core = _NoIO(mapping)
    ctx = AuthzContext(subject(), core=core, db=_NoDB())  # type: ignore[arg-type]
    decision = await build(PATHS[path](gen)).decide(ctx)
    _assert_outcome(decision, expect(outcome), bare=wrap == "bare")
    assert core.asked == [] and counted["resolve"] == 0, row
    if path == "statuses" or not row.startswith("present."):
        assert counted["lookup"] == 0, row


async def test_an_absent_status_never_equals_none(
    gen: Any, counted: dict[str, int]
) -> None:
    """``statuses={"none"}`` (no subscription) does not match a proven-absent
    key (no billing customer): a deny, and ``~`` of it allows only because
    the deny is proven."""
    ctx = AuthzContext(_subject(failed=set()), core=_NoIO(MAP), db=_NoDB())  # type: ignore[arg-type]
    none = gen.Subscribed(frozenset({"none"}), of=Account.user())
    decision = await none.decide(ctx)
    assert not decision.allowed and not decision.indeterminate
    assert decision.http_status == 402
    present = AuthzContext(_subject(failed=set(), status="none"), core=_NoIO(MAP))
    assert (await none.decide(present)).allowed
    assert (await Not(gen.Subscribed(of=Account.user())).decide(ctx)).allowed
    assert counted == {"lookup": 0, "resolve": 0}


async def test_a_remote_billing_denies_a_proven_absence_with_no_call(
    gen: Any, counted: dict[str, int]
) -> None:
    """A split process without billing (its facts contract bound): the
    proven absence is the same 402 on both reads, with no describe call."""
    core = _NoIO(MAP, local=False)
    ctx = AuthzContext(_subject(failed=set()), core=core, db=_NoDB())  # type: ignore[arg-type]
    for build in PATHS.values():
        decision = await build(gen).decide(ctx)
        assert not decision.allowed and not decision.indeterminate
        assert decision.http_status == 402
    assert core.asked == [] and counted == {"lookup": 0, "resolve": 0}


async def test_without_its_owner_an_absence_is_501(gen: Any) -> None:
    """Billing neither loaded nor bound: nobody may vouch, on both reads."""

    class _NoOwner(_NoIO):
        def get_access_fact(self, name: str) -> Any:
            return None

    ctx = AuthzContext(_subject(failed=set()), core=_NoOwner(MAP))
    for build in PATHS.values():
        decision = await Not(build(gen)).decide(ctx)
        assert decision.indeterminate and decision.http_status == 501
        assert decision.code == "fact_unregistered"


async def test_a_previous_authority_of_another_name_proves_no_absence(
    gen: Any,
) -> None:
    """A rollout whose previous authority is another module: a token it
    minted does not prove billing ran, so the absence is 501."""
    from sandloom.core.claims import Authority

    previous = {
        **MAP,
        KEY: Authority(BILLING_PATH, previous=Authority("old", module="old_billing")),
    }
    old_cpv = {"v": CPV_VERSION, "k": {**DIGESTS, KEY: cpv_digest(KEY, "old_billing")}}
    for subject, outcome in (
        (_subject(failed=set(), sources={"identity", "old"}, cpv=old_cpv), 501),
        (_subject(failed=set()), 402),
    ):
        ctx = AuthzContext(subject, core=_NoIO(previous))  # type: ignore[arg-type]
        for build in PATHS.values():
            decision = await build(gen).decide(ctx)
            assert not decision.allowed and decision.http_status == outcome


@pytest.mark.parametrize("enabled", [True, False])
async def test_a_test_kit_token_proves_absence_only_in_a_test_composition(
    gen: Any, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    """The kernel test authority stands for every authority, as the view
    reads it, only in a test composition; elsewhere its token proves no
    absence (501)."""
    monkeypatch.setattr(view, "_test_composition", [enabled])
    vouched = UserContext(
        user_id=uuid4(),
        claims_sources=[view.TEST_AUTHORITY],
        **{CPV_CLAIM: view.TEST_CPV},
    )
    stated = _subject(failed=set(), cpv=view.TEST_CPV)
    for subject in (vouched, stated):
        ctx = AuthzContext(subject, core=_NoIO(MAP))
        for build in PATHS.values():
            decision = await build(gen).decide(ctx)
            assert not decision.allowed
            if enabled:
                assert not decision.indeterminate and decision.http_status == 402
            else:
                assert (decision.http_status, decision.code) == (501, "access_unknown")


async def test_another_declared_claim_key_proves_no_absence() -> None:
    """Only the owner's fact claim may be proven absent: another key the
    owner declares for ``claim_key=`` stays 501 when absent, whatever the
    provenance (never extended beyond the owner's own claim)."""
    fact = AccessFact(
        name="seat.active",
        owner_module="seatco",
        claim_key="seat_status",
        claim_rule=ClaimRule.one_of(("active",)),
        deny_category=Category.PAYMENT_REQUIRED,
        atom="Seated",
        atom_statuses=("active", "none"),
        atom_claim_keys=("seat_status", "seat_status_eu"),
    )

    # The class sandloom generate writes, from the owner's literal.
    literal = {
        "name": "Seated",
        "fact": "seat.active",
        "owner": "seatco",
        "claim_key": "seat_status",
        "claim_keys": ["seat_status", "seat_status_eu"],
        "statuses": ["active", "none"],
        "category": Category.PAYMENT_REQUIRED,
    }
    seated: Any = type(
        "Seated", (ClaimFactCheck,), {ATOM_ATTR: literal, "__module__": __name__}
    )
    mapping = {
        "issuer": "sandloom_identity",
        "roles": "sandloom_identity",
        "permissions": "sandloom_identity",
        "seat_status": "seatco",
        "seat_status_eu": "seatco",
    }

    class _Seat(_NoIO):
        def get_access_fact(self, name: str) -> Any:
            return fact if name == fact.name else None

    authorities = build_claim_authorities(mapping)
    subject = UserContext(
        user_id=uuid4(),
        claims_sources=["identity", "seatco"],
        claims_sources_failed=[],
        **{CPV_CLAIM: {"v": CPV_VERSION, "k": dict(authorities.digests)}},
    )
    ctx = AuthzContext(subject, core=_Seat(mapping))
    for atom, outcome in (
        (seated(of=Account.user()), (402, "Seated")),
        (seated(frozenset({"active"}), of=Account.user()), (402, "Seated")),
        (
            seated(of=Account.user(), claim_key="seat_status_eu"),
            (501, "access_unknown"),
        ),
        (
            seated(frozenset({"none"}), of=Account.user(), claim_key="seat_status_eu"),
            (501, "access_unknown"),
        ),
    ):
        decision = await atom.decide(ctx)
        assert not decision.allowed
        assert (decision.http_status, decision.code) == outcome, atom.claim_key
