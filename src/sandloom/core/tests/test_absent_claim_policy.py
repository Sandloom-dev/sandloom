"""What an absent primary claim means is its fact owner's declaration (KB-33h).

Owner decision 2026-11-09: provenance (``claims_sources``, ``cpv``, the
authority map) proves WHO ran and that it wrote nothing; it does not prove
that absence is a negative fact. The owner declares that on its fact,
``AccessFact(absent_claim="unknown" | "deny")``, default ``"unknown"``,
carried by the generated atom's literal and the facts contract's descriptor,
never inferred from a key or an owner name:

* ``"unknown"`` (default): the authority ran OK at the mint and wrote no
  claim -> 501 ``access_unknown``, and ``~atom`` never allows;
* ``"deny"``: the same proven absence is a definite deny with the FACT's own
  category (403 here; billing's is 402), only for the fact's primary claim
  and only when every provenance condition holds (else 401 / 503 / 501);
* an old literal or descriptor without the field means ``"unknown"``.

A third-party module (``seatco``) with a successful contributor stands for
every owner that is not billing; ``Account.user()`` asks nothing (no
contract, no fact lookup, no live resolution, no SQL), proven by counts.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI

import sandloom.shared.authz.facts as facts
from sandloom.core.app_factory.claim_authorities import build_claim_authorities
from sandloom.core.auth import claims_view as view
from sandloom.core.claims import CPV_CLAIM, CPV_VERSION
from sandloom.core.cli.generated import render_stub
from sandloom.core.contracts.compatibility import compare_contract
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import contract_schema
from sandloom.sdk import contract
from sandloom.shared.authz import (
    AccessFact,
    Account,
    Category,
    ClaimRule,
    From,
    Not,
    Or,
    Role,
    access_check,
)
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.authz.contract_check import ATOM_ATTR, ClaimFactCheck
from sandloom.shared.subject import UserContext

pytestmark = pytest.mark.asyncio

SEATS = "seatco.seats.v1"
ACCOUNT = {"account_type": From.account_type(), "account_id": From.account_id()}

#: A third-party fact that declares nothing about an absent claim.
SEAT_FACT = AccessFact(
    name="seat.active",
    owner_module="seatco",
    claim_key="seat_status",
    claim_rule=ClaimRule.one_of({"active"}),
    deny_category=Category.PAYMENT_REQUIRED,
    atom="Seated",
    atom_statuses=("active", "none"),
    atom_claim_keys=("seat_status", "seat_status_eu"),
)
#: A third-party fact whose owner declares absence a "no", with its own
#: category (403, not billing's 402).
LICENCE_FACT = AccessFact(
    name="licence.held",
    owner_module="seatco",
    claim_key="licence_status",
    claim_rule=ClaimRule.one_of({"held"}),
    deny_category=Category.FORBIDDEN,
    atom="Licensed",
    atom_statuses=("held", "lapsed"),
    atom_claim_keys=("licence_status", "licence_status_eu"),
    absent_claim="deny",
)
FACTS = {SEAT_FACT.name: SEAT_FACT, LICENCE_FACT.name: LICENCE_FACT}
MAP = {
    "issuer": "sandloom_identity",
    "roles": "sandloom_identity",
    "permissions": "sandloom_identity",
    "seat_status": "seatco",
    "seat_status_eu": "seatco",
    "licence_status": "seatco",
    "licence_status_eu": "seatco",
}
CPV: dict[str, Any] = {
    "v": CPV_VERSION,
    "k": dict(build_claim_authorities(MAP).digests),
}
_ABSENT = object()


@contract(SEATS)
class SeatsContract:
    """Seats and licences of an account (a third-party provider)."""

    @access_check("HasSeat", bind=ACCOUNT, claim=SEAT_FACT)
    async def has_seat(self, account_type: str, account_id: UUID) -> bool:
        """True while the account holds a seat."""
        return True

    @access_check("HoldsLicence", bind=ACCOUNT, claim=LICENCE_FACT)
    async def holds_licence(self, account_type: str, account_id: UUID) -> bool:
        """True while the account holds a licence."""
        return True


def _load(source: str, tmp_path: Path, name: str) -> ModuleType:
    path = tmp_path / f"{name}.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gen(tmp_path: Path) -> Any:
    """seatco's stub as ``sandloom generate`` writes it for a consumer."""
    module = _load(render_stub(SEATS, SeatsContract), tmp_path, "_kb33h_seatco_stub")
    yield module
    sys.modules.pop(module.__name__, None)


class _NoIO:
    """A composition with seatco's facts registered locally (a registry
    lookup, not a call) whose contracts may not be asked."""

    def __init__(self, mapping: dict[str, str] | None = MAP) -> None:
        self.core = CoreFacade(app=FastAPI())
        if mapping is not None:
            self.core._assign_claim_authorities(build_claim_authorities(mapping))
        self.asked: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.core, name)

    def get_access_fact(self, name: str) -> Any:
        return FACTS.get(name)

    def remote_fact_owners(self) -> list[str]:
        return []

    def get_service(self, name: str) -> Any:
        self.asked.append(name)
        raise AssertionError(f"Account.user() asked the contract {name!r}")


class _NoDB:
    """A request session nobody may touch (zero SQL)."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"Account.user() used the database ({name})")


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count fact lookups and live resolutions (either fails the count)."""
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
    sources: set[str] | None = None,
    failed: set[str] | None = None,
    cpv: Any = CPV,
    untrusted: dict[str, str] | None = None,
    **claims: Any,
) -> UserContext:
    """seatco's contributor ran OK at the mint (unless ``sources`` /
    ``failed`` say otherwise) and wrote ``claims`` only."""
    failed = set() if failed is None else failed
    ran = {"identity", "seatco"} if sources is None else sources
    data: dict[str, Any] = {
        "user_id": uuid4(),
        "roles": {"member"},
        "claims_sources": ran - failed,
        "claims_sources_failed": failed,
        **claims,
    }
    if cpv is not None:
        data[CPV_CLAIM] = cpv
    if untrusted is not None:
        data[view.UNTRUSTED_KEY] = untrusted
    return UserContext(**data)


def _ctx(subject: UserContext, core: Any = None) -> AuthzContext:
    return AuthzContext(subject, core=core or _NoIO(), db=_NoDB())  # type: ignore[arg-type]


#: Wraps that would allow if a proven absence were read as a deny.
WRAPS: dict[str, Any] = {
    "bare": lambda a: a,
    "not": Not,
    "not_not": lambda a: Not(Not(a)),
    "or_not": lambda a: Or(Role("ops"), Not(a)),
    "and_not": lambda a: Role("member") & Not(a),
    "not_and": lambda a: Not(Role("member") & a),
}
SEAT_PATHS: dict[str, Any] = {
    "default": lambda gen: gen.Seated(of=Account.user()),
    "statuses": lambda gen: gen.Seated(frozenset({"active"}), of=Account.user()),
    "none": lambda gen: gen.Seated(frozenset({"none"}), of=Account.user()),
}


@pytest.mark.parametrize("wrap", sorted(WRAPS))
@pytest.mark.parametrize("path", sorted(SEAT_PATHS))
async def test_a_successful_contributor_without_the_claim_is_501_by_default(
    gen: Any, counted: dict[str, int], path: str, wrap: str
) -> None:
    """Every provenance condition holds, the claim is absent, and seatco
    declares nothing: 501 ``access_unknown`` on every read, never an allow
    through ``Not``; nothing asked."""
    core = _NoIO()
    decision = await WRAPS[wrap](SEAT_PATHS[path](gen)).decide(_ctx(_subject(), core))
    assert not decision.allowed and decision.indeterminate, decision
    assert (decision.http_status, decision.code) == (501, "access_unknown")
    assert core.asked == [] and counted == {"lookup": 0, "resolve": 0}


async def test_a_present_claim_still_decides_by_default(
    gen: Any, counted: dict[str, int]
) -> None:
    """The declaration is about absence only: a vouched status decides."""
    held = _ctx(_subject(seat_status="active"))
    assert (await gen.Seated(of=Account.user()).decide(held)).allowed
    lapsed = await gen.Seated(of=Account.user()).decide(
        _ctx(_subject(seat_status="none"))
    )
    assert not lapsed.allowed and not lapsed.indeterminate
    assert lapsed.http_status == 402
    assert counted["resolve"] == 0


async def test_a_fact_declaring_deny_denies_with_its_own_category(
    gen: Any, counted: dict[str, int]
) -> None:
    """``absent_claim="deny"``: the proven absence is the fact's definite
    deny with ITS category (403 here), and ``~`` of it allows; nothing
    asked."""
    core = _NoIO()
    for atom in (
        gen.Licensed(of=Account.user()),
        gen.Licensed(frozenset({"held"}), of=Account.user()),
        gen.Licensed(frozenset({"lapsed"}), of=Account.user()),
    ):
        decision = await atom.decide(_ctx(_subject(), core))
        assert not decision.allowed and not decision.indeterminate, decision
        assert (decision.http_status, decision.code) == (403, "Licensed")
        assert (await Not(atom).decide(_ctx(_subject(), core))).allowed
    assert core.asked == [] and counted == {"lookup": 0, "resolve": 0}


#: A "deny" fact's absence without every provenance condition: 401 / 503 /
#: 501 as before, never the deny.
UNPROVEN: dict[str, tuple[Any, dict[str, str] | None, tuple[int, str]]] = {
    "failed": (
        lambda: _subject(failed={"seatco"}),
        MAP,
        (503, "claim_authority_failed"),
    ),
    "did_not_run": (
        lambda: _subject(sources={"identity"}),
        MAP,
        (501, "access_unknown"),
    ),
    "no_cpv": (lambda: _subject(cpv=None), MAP, (501, "access_unknown")),
    "unassigned": (
        lambda: _subject(),
        {k: v for k, v in MAP.items() if k != "licence_status"},
        (501, "access_unknown"),
    ),
    "another_authority": (
        lambda: _subject(),
        {**MAP, "licence_status": "sandloom_identity"},
        (501, "access_unknown"),
    ),
    "view_outdated": (
        lambda: _subject(untrusted={"licence_status": view.OUTDATED}),
        MAP,
        (401, view.OUTDATED),
    ),
    "view_mismatch": (
        lambda: _subject(untrusted={"licence_status": view.MISMATCH}),
        MAP,
        (503, view.MISMATCH),
    ),
    "view_source_absent": (
        lambda: _subject(untrusted={"licence_status": view.SOURCE_ABSENT}),
        MAP,
        (501, "access_unknown"),
    ),
}


@pytest.mark.parametrize("row", sorted(UNPROVEN))
async def test_deny_needs_every_provenance_condition(gen: Any, row: str) -> None:
    subject, mapping, (status, code) = UNPROVEN[row]
    core = _NoIO(mapping)
    for atom in (gen.Licensed(of=Account.user()), Not(gen.Licensed(of=Account.user()))):
        decision = await atom.decide(_ctx(subject(), core))
        assert not decision.allowed and decision.indeterminate, (row, decision)
        assert (decision.http_status, decision.code) == (status, code), row
    assert core.asked == []


async def test_deny_applies_only_to_the_primary_claim(gen: Any) -> None:
    """Another key the owner declares for ``claim_key=`` never inherits the
    fact's "deny": absent, it stays 501 whatever the provenance."""
    for atom in (
        gen.Licensed(of=Account.user(), claim_key="licence_status_eu"),
        gen.Licensed(
            frozenset({"lapsed"}), of=Account.user(), claim_key="licence_status_eu"
        ),
    ):
        for policy in (atom, Not(atom)):
            decision = await policy.decide(_ctx(_subject()))
            assert not decision.allowed and decision.indeterminate
            assert (decision.http_status, decision.code) == (501, "access_unknown")


# --------------------------------------------------------------------------- #
# The declaration travels: generated literal, describe, facts contract
# --------------------------------------------------------------------------- #
async def test_the_generated_literal_and_describe_carry_the_declaration(
    gen: Any,
) -> None:
    assert gen.Seated.__sandloom_atom__["absent_claim"] == "unknown"
    assert gen.Licensed.__sandloom_atom__["absent_claim"] == "deny"
    assert gen.Licensed.__sandloom_atom__["category"] == Category.FORBIDDEN
    methods = contract_schema(SeatsContract).describe()["methods"]
    assert methods["has_seat"]["access_check"]["claim"]["absent_claim"] == "unknown"
    assert methods["holds_licence"]["access_check"]["claim"]["absent_claim"] == "deny"


async def test_an_old_literal_without_the_field_means_unknown(
    counted: dict[str, int],
) -> None:
    """A stub generated before KB-33h (no ``absent_claim`` in its literal)
    reads a proven absence as unknown, even for a fact that now declares
    "deny"."""
    literal = {
        "fact": LICENCE_FACT.name,
        "owner": "seatco",
        "claim_key": "licence_status",
        "claim_keys": ["licence_status", "licence_status_eu"],
        "statuses": ["held", "lapsed"],
        "tier": 1,
        "category": Category.FORBIDDEN,
    }
    old: Any = type(
        "Licensed", (ClaimFactCheck,), {ATOM_ATTR: literal, "__module__": __name__}
    )
    for policy in (old(of=Account.user()), Not(old(of=Account.user()))):
        decision = await policy.decide(_ctx(_subject()))
        assert not decision.allowed and decision.indeterminate
        assert (decision.http_status, decision.code) == (501, "access_unknown")
    assert counted == {"lookup": 0, "resolve": 0}


async def test_an_old_descriptor_without_the_field_means_unknown() -> None:
    wire = LICENCE_FACT.to_wire()
    assert wire["absent_claim"] == "deny"
    remote = facts.remote_fact(wire, "seatco.facts.v1")
    assert remote is not None and remote.absent_claim == "deny"
    old = {k: v for k, v in wire.items() if k != "absent_claim"}
    remote = facts.remote_fact(old, "seatco.facts.v1")
    assert remote is not None and remote.absent_claim == "unknown"
    junk = facts.remote_fact({**wire, "absent_claim": "allow"}, "seatco.facts.v1")
    assert junk is not None and junk.absent_claim == "unknown"
    assert SEAT_FACT.to_wire()["absent_claim"] == "unknown"


async def test_absent_claim_takes_only_the_declared_values() -> None:
    with pytest.raises(ValueError, match="absent_claim"):
        AccessFact(name="x.y", owner_module="x", absent_claim="allow")


async def test_withdrawing_deny_is_a_breaking_contract_change() -> None:
    """A consumer whose literal reads absence as the owner's deny must not
    meet a provider that withdrew it; gaining "deny" (or an older
    description without the field) is compatible."""
    expected = contract_schema(SeatsContract).describe()

    def with_absent(doc: Any, method: str, value: str | None) -> Any:
        methods = {k: dict(v) for k, v in doc["methods"].items()}
        check = dict(methods[method]["access_check"])
        claim = dict(check["claim"])
        if value is None:
            claim.pop("absent_claim", None)
        else:
            claim["absent_claim"] = value
        check["claim"] = claim
        methods[method]["access_check"] = check
        return {"methods": methods}  # another document: no shared hash

    withdrawn = with_absent(expected, "holds_licence", "unknown")
    errors = compare_contract(SEATS, expected, withdrawn).errors
    assert any("absent_claim withdrew deny" in e for e in errors), errors
    gained = with_absent(expected, "has_seat", "deny")
    assert compare_contract(SEATS, expected, gained).errors == []
    old = with_absent(expected, "has_seat", None)
    assert compare_contract(SEATS, old, expected).errors == []
