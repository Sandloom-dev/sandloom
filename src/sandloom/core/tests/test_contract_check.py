"""Contract-anchored access checks (ADR-0014 decisions 8 to 12; KB-30).

A third-party provider (``seatco.seats.v1``, no battery involved) declares
its boolean reads with ``access_check``; ``sandloom generate``'s renderer
writes the typed atoms beside the stub, and the module that file is imported
as is what a consumer uses. Pinned here:

* Z-11 generation: the declaration is in the describe document (so in the
  snapshot and the stub's fingerprint), the atoms are rendered in their
  final layout (``ruff format`` leaves them unchanged), twice the same;
* the evaluator: the provider method called once with the bound arguments
  in signature order, a ``False`` the declared category (402 / 403) under
  the atom's class name and label, every wiring-time answer (tier,
  ``contracts()``, ``org_params()``, ``risks()``) read from the literal;
* Z-9 / Z-10 fail-closed: no provider 501 ``service_unavailable``; a shape
  mismatch (arguments refused, method gone, non-boolean result) 501
  ``contract_mismatch`` inside the algebra; an invalid request value refused
  before any call, as the kernel atoms refuse it;
* the claim-tier atom: ``statuses=`` / ``claim_key=`` bound to the owner's
  declaration, the owner's fact deciding a ``Account.user()`` claim with zero
  calls, 501 with the owner neither loaded nor bound (owner #4), the live
  check for an organization account;
* the compatibility rule: removing, re-binding, narrowing or lowering the
  risk of a declared check is incompatible (T004); adding one, or a new
  label, is not;
* a declaration that cannot be an access check is refused at registration;
* ``Account`` neutrality (C4): the third party's check targets
  ``Account.org(param=)`` and ``Account.bound_tenant()`` with no billing in
  the composition;
* a generated atom's contract is access-bearing (KB-13 source (c)).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from sandloom.core.cli import dto_copies as _dto_copies
from sandloom.core.cli.generated import render_stub
from sandloom.core.contract_wire import ContractTypeError
from sandloom.core.contracts.compatibility import compare_contract
from sandloom.core.di import bind_tenant
from sandloom.core.module_api import contract_schema
from sandloom.core.module_api._errors import ContractArgumentError
from sandloom.core.module_api._service_registry import ServiceUnavailableInDeployment
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
from sandloom.shared.authz.algebra import TIER_CLAIM, TIER_LIVE
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.authz.contract_check import (
    CONTRACT_MISMATCH,
    generated_access_contracts,
)
from sandloom.shared.subject import UserContext

pytestmark = pytest.mark.asyncio

SEATS = "seatco.seats.v1"
MEMBERSHIP = "tenancy.membership.v1"

SEAT_FACT = AccessFact(
    name="seat.active",
    owner_module="seatco",
    claim_key="seat_status",
    claim_rule=ClaimRule.one_of({"active", "grace"}),
    deny_category=Category.PAYMENT_REQUIRED,
    atom="Seated",
    atom_statuses=("active", "grace", "none"),
    atom_claim_keys=("seat_status", "seat_status_eu"),
    atom_describe="a seat",
)


@contract(SEATS)
class SeatsContract:
    """Seat licences of an account (a third-party provider)."""

    @access_check(
        "HasSeat",
        bind={"account_type": From.account_type(), "account_id": From.account_id()},
        category=Category.PAYMENT_REQUIRED,
        describe="a seat",
        claim=SEAT_FACT,
    )
    async def has_seat(self, account_type: str, account_id: UUID) -> bool:
        """True while the account holds a seat."""
        return True

    @access_check(
        "SeatRole",
        bind={
            "organization_id": From.org(param="org_id"),
            "user_id": From.subject_id(),
        },
        values={"level": ["viewer", "editor", "owner"]},
        risk={"admin": {"level": ["owner"]}},
        describe="seat level '{level}'",
    )
    async def has_level(
        self, organization_id: UUID, user_id: UUID, level: str, *, region: str = "eu"
    ) -> bool:
        """True when the user's seat in the organization ranks at least ``level``."""
        return True

    async def count(self, account_id: UUID) -> int:
        """Seats in use (no access check)."""
        return 0


def _generated(tmp_path: Path, name: str = "seatco_seats_v1") -> ModuleType:
    """The stub ``sandloom generate`` writes, imported as a consumer imports it."""
    path = tmp_path / f"{name}.py"
    path.write_text(render_stub(SEATS, SeatsContract))
    spec = importlib.util.spec_from_file_location(f"_kb30_{name}", path)
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


class _Seats:
    """A recording provider: what was ASKED, in order."""

    def __init__(self, answer: Any = True) -> None:
        self.answer = answer
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def has_seat(self, account_type: str, account_id: UUID) -> Any:
        self.calls.append(("has_seat", (account_type, account_id)))
        return self.answer

    async def has_level(
        self, organization_id: UUID, user_id: UUID, level: str, region: str = "eu"
    ) -> Any:
        self.calls.append(("has_level", (organization_id, user_id, level, region)))
        return self.answer


class _Core:
    def __init__(self, services: dict[str, Any], facts: dict[str, Any] | None = None):
        self.services = services
        self.facts = facts or {}
        self.owners: list[str] = []

    def get_service(self, name: str) -> Any:
        if name not in self.services:
            raise ServiceUnavailableInDeployment(name)
        return self.services[name]

    def get_access_fact(self, name: str) -> Any:
        return self.facts.get(name)

    def remote_fact_owners(self) -> list[str]:
        return list(self.owners)

    def get_metric(self, name: str) -> Any:
        return None


class _Req:
    def __init__(
        self, path: dict[str, str] | None = None, headers: dict[str, str] | None = None
    ) -> None:
        self.path_params = path or {}
        self.query_params: dict[str, str] = {}
        self.headers = headers or {}
        self.scope: dict[str, Any] = {"state": {}}
        self.state = SimpleNamespace()


def _ctx(core: Any, *, request: Any = None, **claims: Any) -> AuthzContext:
    subject = UserContext(
        user_id=claims.pop("user_id", uuid4()),
        tenant_ids=[str(t) for t in claims.pop("tenants", [])],
        claims=claims,
        claims_sources=["seatco"],
        claims_sources_failed=[],
    )
    return AuthzContext(subject, request=request, core=core)


# --------------------------------------------------------------------------- #
# Z-11 generation
# --------------------------------------------------------------------------- #
async def test_the_declaration_is_part_of_the_describe_document() -> None:
    methods = contract_schema(SeatsContract).describe()["methods"]
    declared = methods["has_level"]["access_check"]
    assert declared["name"] == "SeatRole"
    assert declared["bind"]["organization_id"] == ["org", "org_id", True]
    assert declared["risk"] == {"admin": {"level": ["owner"]}}
    claim = methods["has_seat"]["access_check"]["claim"]
    assert claim["name"] == "Seated" and claim["owner"] == "seatco"
    assert claim["claim_keys"] == ["seat_status", "seat_status_eu"]
    assert claim["statuses"] == ["active", "grace", "none"]
    assert "access_check" not in methods["count"]


async def test_the_atoms_are_generated_in_their_final_layout(tmp_path: Path) -> None:
    first = render_stub(SEATS, SeatsContract)
    assert first == render_stub(SEATS, SeatsContract)
    for name in ("class HasSeat(ContractCheck):", "class SeatRole(ContractCheck):"):
        assert name in first
    assert "class Seated(ClaimFactCheck):" in first
    assert 'level: Literal["viewer", "editor", "owner"]' in first
    assert "# sandloom: advanced generated code" in first
    path = tmp_path / "seatco_seats_v1.py"
    assert _dto_copies.ruff_formatted(first, path, cwd=Path.cwd()) == first


async def test_the_generated_signature_is_the_atoms_own(gen: Any) -> None:
    import inspect

    params = inspect.signature(gen.SeatRole.__init__).parameters
    assert list(params) == ["self", "level", "region", "org_param"]
    assert params["org_param"].kind is inspect.Parameter.KEYWORD_ONLY
    assert list(inspect.signature(gen.HasSeat.__init__).parameters) == ["self", "of"]
    seated = inspect.signature(gen.Seated.__init__).parameters
    assert list(seated) == ["self", "statuses", "of", "claim_key"]


# --------------------------------------------------------------------------- #
# The evaluator
# --------------------------------------------------------------------------- #
async def test_the_provider_is_called_once_in_signature_order(gen: Any) -> None:
    seats = _Seats()
    org, user = uuid4(), uuid4()
    core = _Core({SEATS: seats})
    atom = gen.SeatRole("editor", region="us")
    ctx = _ctx(
        core, request=_Req(path={"org_id": str(org)}), user_id=user, tenants=[org]
    )
    assert (await atom.decide(ctx)).allowed
    assert seats.calls == [("has_level", (org, user, "editor", "us"))]


async def test_wiring_answers_come_from_the_literal(gen: Any) -> None:
    owner = gen.SeatRole("owner", org_param="team_id")
    assert owner.tier == TIER_LIVE
    assert owner.contracts() == frozenset({SEATS})
    assert owner.org_params() == frozenset({"team_id"})
    assert owner.risks() == frozenset({"admin"})
    assert gen.SeatRole("viewer").risks() == frozenset()
    assert Not(owner).risks() == frozenset()
    assert gen.HasSeat(of=Account.org(param="org_id")).org_params() == {"org_id"}
    with pytest.raises(ValueError, match="level must be one of"):
        gen.SeatRole("admin")


async def test_a_false_answer_is_the_declared_category(gen: Any) -> None:
    core = _Core({SEATS: _Seats(answer=False)})
    decision = await gen.HasSeat(of=Account.user()).decide(_ctx(core))
    assert not decision.allowed and not decision.indeterminate
    assert decision.http_status == 402 and decision.code == "HasSeat"
    assert decision.failed_requirement == "a seat"
    org = uuid4()
    ctx = _ctx(core, request=_Req(path={"org_id": str(org)}), tenants=[org])
    denied = await gen.SeatRole("viewer").decide(ctx)
    assert denied.http_status == 403 and denied.reason == "requires seat level 'viewer'"


async def test_no_provider_is_501_even_negated(gen: Any) -> None:
    decision = await Not(gen.HasSeat(of=Account.user())).decide(_ctx(_Core({})))
    assert decision.indeterminate and decision.http_status == 501
    assert decision.code == "service_unavailable"


class _Refusing(_Seats):
    async def has_seat(self, account_type: str, account_id: UUID) -> Any:
        raise ContractArgumentError(
            status_code=422, detail="missing a required argument: 'scope'"
        )


@pytest.mark.parametrize(
    "provider",
    [_Refusing(), _Seats(answer=1), _Seats(answer=None), SimpleNamespace()],
    ids=["arguments-refused", "int-result", "none-result", "method-gone"],
)
async def test_a_shape_mismatch_is_501_inside_the_algebra(
    gen: Any, provider: Any
) -> None:
    core = _Core({SEATS: provider})
    atom = gen.HasSeat(of=Account.user())
    decision = await atom.decide(_ctx(core))
    assert decision.indeterminate and decision.http_status == 501
    assert decision.code == CONTRACT_MISMATCH
    assert (await Not(atom).decide(_ctx(core))).indeterminate
    allowed = await Or(atom, Role("admin")).decide(_ctx(core, user_id=uuid4()))
    assert not allowed.allowed  # no genuine allow: still never an error
    subject = UserContext(user_id=uuid4(), roles=["admin"])
    assert (
        await Or(atom, Role("admin")).decide(AuthzContext(subject, core=core))
    ).allowed


async def test_an_invalid_request_value_is_refused_before_any_call(gen: Any) -> None:
    seats = _Seats()
    ctx = _ctx(_Core({SEATS: seats}), request=_Req(path={"org_id": "not-a-uuid"}))
    decision = await gen.SeatRole("viewer").decide(ctx)
    assert decision.indeterminate and decision.http_status == 400
    assert decision.code == "invalid_tenant" and seats.calls == []


# --------------------------------------------------------------------------- #
# Account neutrality (owner confirmation C4): no billing anywhere
# --------------------------------------------------------------------------- #
async def test_a_third_party_check_targets_every_account_without_billing(
    gen: Any,
) -> None:
    seats = _Seats()
    org, user = uuid4(), uuid4()

    async def has_role_at_least(o: UUID, u: UUID, minimum: str) -> bool:
        return o == org and u == user and minimum == "member"

    membership = SimpleNamespace(has_role_at_least=has_role_at_least)
    core = _Core({SEATS: seats, MEMBERSHIP: membership})
    req = _Req(path={"org_id": str(org)})
    ctx = _ctx(core, request=req, user_id=user, tenants=[org])
    assert (await gen.HasSeat(of=Account.org(param="org_id")).decide(ctx)).allowed
    with bind_tenant(org):
        bound = _ctx(core, request=_Req(), user_id=user, tenants=[org])
        assert (await gen.HasSeat(of=Account.bound_tenant()).decide(bound)).allowed
    assert seats.calls == [
        ("has_seat", ("organization", org)),
        ("has_seat", ("organization", org)),
    ]
    with bind_tenant(user):  # the caller's own tenant: the personal account
        personal = _ctx(core, request=_Req(), user_id=user)
        assert (await gen.HasSeat(of=Account.bound_tenant()).decide(personal)).allowed
    assert seats.calls[-1] == ("has_seat", ("user", user))


# --------------------------------------------------------------------------- #
# The claim-tier atom (owner decisions #4, #5)
# --------------------------------------------------------------------------- #
async def test_the_claim_atom_reads_the_owners_fact_with_no_call(gen: Any) -> None:
    seats = _Seats()
    core = _Core({SEATS: seats}, facts={"seat.active": SEAT_FACT})
    atom = gen.Seated(of=Account.user())
    assert atom.tier == TIER_CLAIM
    assert atom.contracts() == frozenset({"seatco.facts.v1"})
    assert (await atom.decide(_ctx(core, seat_status="grace"))).allowed
    denied = await atom.decide(_ctx(core, seat_status="none"))
    assert denied.http_status == 402 and denied.code == "Seated"
    assert seats.calls == []


async def test_explicit_statuses_and_keys_stay_within_the_declaration(gen: Any) -> None:
    core = _Core({}, facts={"seat.active": SEAT_FACT})
    strict = gen.Seated(frozenset({"active"}), of=Account.user())
    assert not (await strict.decide(_ctx(core, seat_status="grace"))).allowed
    eu = gen.Seated(of=Account.user(), claim_key="seat_status_eu")
    assert (await eu.decide(_ctx(core, seat_status_eu="active"))).allowed
    with pytest.raises(ValueError, match="statuses must be among"):
        gen.Seated(frozenset({"paid"}), of=Account.user())
    with pytest.raises(ValueError, match="claim_key must be one of"):
        gen.Seated(of=Account.user(), claim_key="is_admin")


async def test_without_its_owner_the_claim_atom_is_501(gen: Any) -> None:
    core = _Core({})
    for atom in (
        gen.Seated(of=Account.user()),
        gen.Seated(frozenset({"active"}), of=Account.user()),
    ):
        decision = await Not(atom).decide(_ctx(core, seat_status="active"))
        assert decision.indeterminate and decision.http_status == 501
        assert decision.code == "fact_unregistered"
    core.owners = ["seatco"]  # bound remotely: the verbatim path needs no call
    strict = gen.Seated(frozenset({"active"}), of=Account.user())
    assert (await strict.decide(_ctx(core, seat_status="active"))).allowed


async def test_an_organization_account_asks_the_live_check(gen: Any) -> None:
    seats = _Seats(answer=False)
    org = uuid4()
    core = _Core({SEATS: seats}, facts={"seat.active": SEAT_FACT})
    atom = gen.Seated(of=Account.org())
    assert atom.tier == TIER_LIVE
    assert atom.contracts() == frozenset({"seatco.facts.v1", SEATS})
    ctx = _ctx(core, request=_Req(path={"org_id": str(org)}), tenants=[org])
    decision = await atom.decide(ctx)
    assert decision.http_status == 402 and seats.calls == [
        ("has_seat", ("organization", org))
    ]


# --------------------------------------------------------------------------- #
# Compatibility (SANDLOOM-T004) and registration refusals
# --------------------------------------------------------------------------- #
def _describe_with(change: Any) -> dict[str, Any]:
    doc = contract_schema(SeatsContract).describe()
    actual = {"methods": {k: dict(v) for k, v in doc["methods"].items()}}
    check = dict(actual["methods"]["has_level"]["access_check"])
    actual["methods"]["has_level"]["access_check"] = change(check)
    return actual


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        (lambda c: None, "removed by the provider"),
        (lambda c: {**c, "bind": {"organization_id": ["subject"]}}, "bind changed"),
        (lambda c: {**c, "category": "payment_required"}, "category changed"),
        (lambda c: {**c, "risk": {}}, "risk 'admin' removed"),
        (lambda c: {**c, "risk": {"admin": {"level": []}}}, "lowered for 'level'"),
        (lambda c: {**c, "values": {"level": ["viewer"]}}, "lost editor, owner"),
    ],
    ids=["removed", "re-bound", "category", "risk-removed", "risk-lowered", "narrowed"],
)
async def test_a_breaking_access_check_change_is_incompatible(
    change: Any, fragment: str
) -> None:
    expected = contract_schema(SeatsContract).describe()
    report = compare_contract(SEATS, expected, _describe_with(change))
    assert any(fragment in e for e in report.errors), report.errors


async def test_a_new_label_or_a_wider_check_is_compatible() -> None:
    expected = contract_schema(SeatsContract).describe()

    def wider(c: dict[str, Any]) -> dict[str, Any]:
        return {
            **c,
            "describe": "seat level at least '{level}'",
            "values": {"level": ["viewer", "editor", "owner", "guest"]},
            "risk": {"admin": {"level": ["owner", "editor"]}},
        }

    assert compare_contract(SEATS, expected, _describe_with(wider)).errors == []
    without = {
        "methods": {
            name: {k: v for k, v in method.items() if k != "access_check"}
            for name, method in expected["methods"].items()
        }
    }
    added = compare_contract(SEATS, without, expected)
    assert added.errors == []


async def test_a_declaration_that_is_not_a_boolean_read_is_refused() -> None:
    class Broken:
        @access_check("Count", bind={"account": From.account_id()})
        async def count(self, account_id: UUID) -> int:
            return 0

    with pytest.raises(ContractTypeError) as exc:
        contract_schema(contract("seatco.broken.v1")(Broken))
    text = str(exc.value)
    assert "returns <class 'int'>, not bool" in text
    assert "binds 'account', which is not a parameter" in text
    with pytest.raises(ValueError, match="not a From source"):
        access_check("Bad", bind={"x": ["somewhere"]})
    with pytest.raises(ValueError, match="class name"):
        access_check("bad", bind={})


# --------------------------------------------------------------------------- #
# Access-bearing (ADR-0014 decision 6, KB-13 source (c))
# --------------------------------------------------------------------------- #
async def test_a_generated_atoms_contract_is_access_bearing(gen: Any) -> None:
    assert SEATS in generated_access_contracts()
