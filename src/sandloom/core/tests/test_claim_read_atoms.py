"""Claim atoms over a mapping claim (``claim_atom``; ADR-0014 decision 15; KB-33).

``Feature`` and ``WithinLimit`` move out of the kernel as generated atoms of
their owner's access checks. A third-party provider (``planco.plans.v1``, no
battery involved) declares both exactly as billing does; the decisions of
the atoms ``sandloom generate``'s renderer writes beside the stub are pinned
as written-out expectations (the kernel atoms' decisions wherever a source
speaks, KB-34b; no kernel atom is evaluated). Pinned here:

* generation: the declaration is in the describe document, the atoms are
  rendered in their final layout with the kernel atoms' signatures
  (``Feature(key, *, of, claim_key)``, ``WithinLimit(key, used, *, of,
  claim_key)``), a malformed declaration is refused;
* ``Account.user()``: the pinned decision of every atom on every subject
  (present, an authority that failed at the mint 503, the view's 401 / 503),
  under ``Not`` and ``Or`` too, with zero calls and no contract named;
* ``Account.user()`` three-valued from the token (KB-33d, G-AUTHZ-12 as
  amended, the owner's table): a present map is the complete snapshot (an
  explicit on allows, an explicit off or an unlisted entry denies); the
  whole claim absent with no failed authority, unvouched by the view, or
  malformed is 501 ``access_unknown`` where the kernel atom denied (so
  ``Not`` allowed); a failed authority 503; never an allow under ``Not``,
  ``Or`` or ``And``, and nothing asked;
* an organization account: the provider's live check asked once with the
  atom's parameters, the pinned decisions and calls; no provider is 501 and
  ``Not`` never allows;
* no source for the answer (KB-33c, C-B5): a provider raising
  ``AccessUnknown`` is indeterminate 501 ``access_unknown``; ``Not`` and an
  ``Or`` of such branches never allow;
* a third-party source (``acme.plans.v1``, no battery, no kernel change):
  the same decisions in-process and over the real internal RPC, an explicit
  off / exceeded limit a deny, an unknown 501;
* the compatibility rule: changing a claim atom's read is incompatible.
"""

from __future__ import annotations

import copy
import importlib.util
import inspect
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI

from sandloom.core.app_factory.claim_authorities import build_claim_authorities
from sandloom.core.auth import claims_view as view
from sandloom.core.cli import dto_copies as _dto_copies
from sandloom.core.cli.generated import render_stub
from sandloom.core.contract_testing import (
    in_process_rpc_client,
    rpc_proxy_for,
    rpc_settings,
)
from sandloom.core.contracts.compatibility import compare_contract
from sandloom.core.facade import CoreFacade
from sandloom.core.idempotency import cache_store as idempotency_cache_store
from sandloom.core.module_api import contract_schema
from sandloom.core.module_api._service_registry import ServiceUnavailableInDeployment
from sandloom.core.rpc.errors import RemoteServiceUnavailable
from sandloom.core.testing import InMemoryCache
from sandloom.sdk import contract
from sandloom.shared.authz import (
    AccessUnknown,
    Account,
    Category,
    From,
    Not,
    Or,
    Role,
    access_check,
    claim_atom,
)
from sandloom.shared.authz.algebra import TIER_CLAIM, TIER_LIVE
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.subject import UserContext

pytestmark = pytest.mark.asyncio

PLANS_CONTRACT = "planco.plans.v1"
IDP = "acme.idp"
PLANS = "acme.plans"
MAP = {
    "issuer": IDP,
    "roles": IDP,
    "permissions": IDP,
    "features": PLANS,
    "limits": PLANS,
}
UNASSIGNED_MAP = {"issuer": IDP, "roles": IDP, "permissions": IDP}


@contract(PLANS_CONTRACT)
class PlansContract:
    """Plan entitlements of an account (a third-party provider)."""

    @claim_atom("Feature", claim_key="features", entry="key")
    @access_check(
        "LiveFeature",
        bind={"tenant_type": From.account_type(), "tenant_id": From.account_id()},
        describe="feature '{key}'",
    )
    async def has_feature(self, tenant_type: str, tenant_id: UUID, key: str) -> bool:
        """True when the plan enables ``key``."""
        return False

    @claim_atom("WithinLimit", claim_key="limits", entry="key", below="used")
    @access_check(
        "LiveWithinLimit",
        bind={"tenant_type": From.account_type(), "tenant_id": From.account_id()},
        category=Category.QUOTA_EXCEEDED,
        describe="within limit '{key}'",
    )
    async def is_within_limit(
        self, tenant_type: str, tenant_id: UUID, key: str, used: int
    ) -> bool:
        """True when ``used`` is below the plan limit ``key``."""
        return False


def _generated(
    tmp_path: Path,
    name: str = PLANS_CONTRACT,
    provider: type = PlansContract,
    as_name: str = "_kb33_planco_plans_v1",
) -> ModuleType:
    path = tmp_path / f"{name.replace('.', '_')}.py"
    path.write_text(render_stub(name, provider))
    spec = importlib.util.spec_from_file_location(as_name, path)
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


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #
async def test_the_declaration_is_part_of_the_describe_document() -> None:
    methods = contract_schema(PlansContract).describe()["methods"]
    feature = methods["has_feature"]["access_check"]["claim"]
    assert feature == {
        "name": "Feature",
        "read": "flag",
        "claim_key": "features",
        "claim_keys": ["features"],
        "entry": "key",
        "category": "forbidden",
        "describe": "feature '{key}'",
    }
    limit = methods["is_within_limit"]["access_check"]["claim"]
    assert limit["read"] == "limit" and limit["below"] == "used"
    assert limit["category"] == Category.QUOTA_EXCEEDED


async def test_the_atoms_are_generated_in_their_final_layout(tmp_path: Path) -> None:
    first = render_stub(PLANS_CONTRACT, PlansContract)
    assert first == render_stub(PLANS_CONTRACT, PlansContract)
    assert "class Feature(ClaimReadCheck):" in first
    assert "class WithinLimit(ClaimReadCheck):" in first
    path = tmp_path / "planco_plans_v1.py"
    assert _dto_copies.ruff_formatted(first, path, cwd=Path.cwd()) == first


async def test_the_signatures_are_the_kernel_atoms(gen: Any) -> None:
    """The kernel atoms' signatures, written out: a call site keeps working
    when it imports the generated atom instead."""
    positional = inspect.Parameter.POSITIONAL_OR_KEYWORD
    keyword = inspect.Parameter.KEYWORD_ONLY
    expected = {
        "Feature": [
            ("self", positional),
            ("key", positional),
            ("of", keyword),
            ("claim_key", keyword),
        ],
        "WithinLimit": [
            ("self", positional),
            ("key", positional),
            ("used", positional),
            ("of", keyword),
            ("claim_key", keyword),
        ],
    }
    for name, params in expected.items():
        mine = inspect.signature(getattr(gen, name).__init__).parameters
        assert [(p, v.kind) for p, v in mine.items()] == params, name


async def test_a_malformed_declaration_is_refused() -> None:
    with pytest.raises(ValueError, match="above @access_check"):

        @claim_atom("Feature", claim_key="features", entry="key")
        async def bare(key: str) -> bool:  # pragma: no cover - refused
            return False

    with pytest.raises(ValueError, match="class name"):
        claim_atom("feature", claim_key="features", entry="key")

    with pytest.raises(ValueError, match="must bind"):

        @claim_atom("Feature", claim_key="features", entry="key")
        @access_check("LiveFeature", bind={"user_id": From.subject_id()})
        async def by_subject(user_id: UUID, key: str) -> bool:  # pragma: no cover
            return False

    @contract("planco.broken.v1")
    class Broken:
        @claim_atom("Feature", claim_key="features", entry="name")
        @access_check(
            "LiveFeature",
            bind={"tenant_type": From.account_type(), "tenant_id": From.account_id()},
        )
        async def has_feature(
            self, tenant_type: str, tenant_id: UUID, key: str
        ) -> bool:
            return False

    with pytest.raises(Exception, match="claim atom entry names 'name'"):
        contract_schema(Broken)


# --------------------------------------------------------------------------- #
# Account.user(): the claims, zero I/O, the pinned decisions
# --------------------------------------------------------------------------- #
class _Provider:
    """A recording provider of planco.plans.v1 (the test producer): what
    was ASKED, in order, answered from a plan table."""

    def __init__(self, plans: dict[Any, dict[str, Any]] | None = None) -> None:
        self.plans = plans or {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def has_feature(self, tenant_type: str, tenant_id: UUID, key: str) -> bool:
        self.calls.append(("has_feature", (tenant_type, tenant_id, key)))
        plan = self.plans.get(tenant_id, {})
        return plan.get("features", {}).get(key) is True

    async def is_within_limit(
        self, tenant_type: str, tenant_id: UUID, key: str, used: int
    ) -> bool:
        self.calls.append(("is_within_limit", (tenant_type, tenant_id, key, used)))
        limit = self.plans.get(tenant_id, {}).get("limits", {}).get(key)
        return isinstance(limit, int) and used < limit


def _core(mapping: dict[str, str] | None = MAP) -> CoreFacade:
    core = CoreFacade(app=FastAPI())
    if mapping is not None:
        core._assign_claim_authorities(build_claim_authorities(mapping))
    return core


CLAIMS: dict[str, Any] = {
    "features": {"export": True, "beta": False, "truthy": 1},
    "limits": {"seats": 3, "text": "3", "flag": True, "zero": 0},
}


def _subject(
    *, failed: frozenset[str] | set[str] | None = frozenset(), **claims: Any
) -> UserContext:
    data: dict[str, Any] = {"user_id": uuid4(), "roles": {"member"}, **claims}
    if failed is not None:
        data["claims_sources"] = {"idp", "plans"} - set(failed)
        data["claims_sources_failed"] = set(failed)
    return UserContext(**data)


def _viewed(key: str, reason: str) -> UserContext:
    return UserContext(
        user_id=uuid4(),
        claims_sources={"idp", "plans"},
        claims_sources_failed=set(),
        **{view.UNTRUSTED_KEY: {key: reason}},
    )


SUBJECTS: dict[str, tuple[Any, dict[str, str] | None]] = {
    "present": (lambda: _subject(**CLAIMS), MAP),
    "present.rogue_failed": (lambda: _subject(failed={"rogue"}, **CLAIMS), MAP),
    "present.authority_failed": (lambda: _subject(failed={"plans"}, **CLAIMS), MAP),
    "empty": (lambda: _subject(features={}, limits={}), MAP),
    "absent.ran": (lambda: _subject(), MAP),
    "absent.failed": (lambda: _subject(failed={"plans"}), MAP),
    "absent.failed.unassigned": (lambda: _subject(failed={"plans"}), UNASSIGNED_MAP),
    "absent.failed.no_map": (lambda: _subject(failed={"plans"}), None),
    "absent.legacy": (lambda: _subject(failed=None), MAP),
    "present.legacy": (lambda: _subject(failed=None, **CLAIMS), MAP),
    "malformed": (lambda: _subject(features=["export"], limits=3), MAP),
    **{
        f"view.{key}.{reason}": (lambda k=key, r=reason: _viewed(k, r), MAP)
        for key in ("features", "limits")
        for reason in (
            view.OUTDATED,
            view.MISMATCH,
            view.UNASSIGNED,
            view.SOURCE_ABSENT,
        )
    },
}

USER_ATOMS: dict[str, Any] = {
    "feature.on": lambda k: k.Feature("export", of=Account.user()),
    "feature.off": lambda k: k.Feature("beta", of=Account.user()),
    "feature.truthy": lambda k: k.Feature("truthy", of=Account.user()),
    "feature.missing": lambda k: k.Feature("nope", of=Account.user()),
    "limit.below": lambda k: k.WithinLimit("seats", 2, of=Account.user()),
    "limit.equal": lambda k: k.WithinLimit("seats", 3, of=Account.user()),
    "limit.text": lambda k: k.WithinLimit("text", 0, of=Account.user()),
    "limit.bool": lambda k: k.WithinLimit("flag", 0, of=Account.user()),
    "limit.zero": lambda k: k.WithinLimit("zero", -1, of=Account.user()),
    "limit.missing": lambda k: k.WithinLimit("nope", 0, of=Account.user()),
}
WRAPS: dict[str, Any] = {
    "bare": lambda a: a,
    "not": Not,
    "or_denied": lambda a: Or(Role("ops"), a),
    "or_allowed": lambda a: Or(Role("member"), a),
}


def _key(decision: Any) -> tuple[Any, ...]:
    return (
        decision.allowed,
        decision.http_status,
        decision.category,
        decision.code,
        decision.indeterminate,
        decision.failed_requirement,
    )


#: The pinned decisions (KB-34b: written out, no kernel atom evaluated). An
#: outcome is ``"allow"``, ``"deny"`` (definite) or the indeterminate
#: ``(status, code)``. Wherever a source speaks they are the kernel atoms'
#: decisions; where none does (KB-33d, the owner's decision of 2026-11-09,
#: G-AUTHZ-12 as amended) the kernel atom denied, so ``Not`` allowed, and the
#: generated atom answers 501 ``access_unknown``.
UNKNOWN = (501, "access_unknown")
AUTHORITY_FAILED = (503, "claim_authority_failed")
#: A present map (:data:`CLAIMS`) is the complete snapshot, whatever else
#: failed and with or without provenance: an entry of the wrong type (a flag
#: ``1``, a limit ``"3"`` or ``True``) is malformed, 501.
PRESENT_OUTCOMES: dict[str, Any] = {
    "feature.on": "allow",
    "feature.off": "deny",
    "feature.truthy": UNKNOWN,
    "feature.missing": "deny",
    "limit.below": "allow",
    "limit.equal": "deny",
    "limit.text": UNKNOWN,
    "limit.bool": UNKNOWN,
    "limit.zero": "allow",
    "limit.missing": "deny",
}
#: Subjects every atom answers alike: an empty map denies every entry; a
#: claim absent with no failed authority to blame (or malformed) is 501; an
#: absent claim whose authority failed (or may have, with no map) is 503.
UNIFORM_OUTCOMES: dict[str, Any] = {
    "empty": "deny",
    "absent.ran": UNKNOWN,
    "absent.failed": AUTHORITY_FAILED,
    "absent.failed.unassigned": UNKNOWN,
    "absent.failed.no_map": AUTHORITY_FAILED,
    "absent.legacy": UNKNOWN,
    "malformed": UNKNOWN,
}
#: The view removed one key for a reason: an atom of that key answers the
#: view's 401 / 503, or 501 for a reason that is no source's word; an atom of
#: the other key finds no claim at all, 501.
VIEW_OUTCOMES: dict[str, Any] = {
    view.OUTDATED: (401, view.OUTDATED),
    view.MISMATCH: (503, view.MISMATCH),
    view.UNASSIGNED: UNKNOWN,
    view.SOURCE_ABSENT: UNKNOWN,
}
#: The category of each indeterminate status, and each kind's definite deny.
INDETERMINATE_CATEGORY = {
    401: Category.UNAUTHENTICATED,
    501: Category.POLICY_MISCONFIGURED,
    503: Category.POLICY_MISCONFIGURED,
}
DENIAL = {
    "feature": ("Feature", 403, Category.FORBIDDEN),
    "limit": ("WithinLimit", 402, Category.QUOTA_EXCEEDED),
}


def _expected(atom: str, subject: str) -> Any:
    """The pinned outcome of the bare ``atom`` on ``subject``."""
    if subject.startswith("view."):
        _, key, reason = subject.split(".", 2)
        mine = key == ("features" if atom.startswith("feature.") else "limits")
        return VIEW_OUTCOMES[reason] if mine else UNKNOWN
    if subject.startswith("present"):
        return PRESENT_OUTCOMES[atom]
    return UNIFORM_OUTCOMES[subject]


def _expected_key(atom: str, outcome: Any, label: str) -> tuple[Any, ...]:
    """The whole bare decision (:func:`_key`) an outcome stands for."""
    if outcome == "allow":
        return (True, 200, None, "ok", False, None)
    if outcome == "deny":
        code, status, category = DENIAL[atom.split(".", 1)[0]]
        return (False, status, category, code, False, label)
    status, code = outcome
    return (False, status, INDETERMINATE_CATEGORY[status], code, True, label)


def _wrapped(wrap: str, outcome: Any, subject: str) -> Any:
    """How a wrap maps the bare outcome: ``Not`` swaps a definite answer and
    keeps an indeterminate; ``Role("member")`` beside it in an ``Or`` allows
    on every subject holding the role (all but the viewed ones, which carry
    no claim at all), else the ``Or`` is the atom's own answer."""
    if wrap == "not":
        return {"allow": "deny", "deny": "allow"}.get(outcome, outcome)
    if wrap == "or_allowed" and not subject.startswith("view."):
        return "allow"
    return outcome


@pytest.mark.parametrize("wrap", sorted(WRAPS))
@pytest.mark.parametrize("atom", sorted(USER_ATOMS))
async def test_the_user_account_decides_as_the_kernel_atom(
    gen: Any, atom: str, wrap: str
) -> None:
    """The pinned decision on every subject: the kernel atom's wherever a
    source speaks (present, empty, the view's 401 / 503, an authority that
    failed); where none does, 501 ``access_unknown``, never an allow (only a
    reliable sibling of an ``Or`` allows), with zero calls on every row."""
    provider = _Provider()
    for name, (subject, mapping) in SUBJECTS.items():
        core = _core(mapping)
        core.get_service = lambda _n: provider  # type: ignore[method-assign,assignment]
        ctx = AuthzContext(subject(), core=core)
        built = USER_ATOMS[atom](gen)
        decision = await WRAPS[wrap](built).decide(ctx)
        outcome = _wrapped(wrap, _expected(atom, name), name)
        if wrap == "bare":
            expected = _expected_key(atom, outcome, built.describe())
            assert _key(decision) == expected, name
        elif outcome == "allow":
            assert decision.allowed and not decision.indeterminate, name
        elif outcome == "deny":
            assert not decision.allowed and not decision.indeterminate, name
        else:
            assert not decision.allowed and decision.indeterminate, name
            assert (decision.http_status, decision.code) == outcome, name
    assert provider.calls == []


async def test_the_trust_cases_keep_their_outcomes(gen: Any) -> None:
    """The comparison above is not vacuous: the claim atom allows, denies and
    answers each indeterminate."""
    seen = {}
    for name in ("present", "absent.ran", "absent.failed", "absent.failed.no_map"):
        subject, mapping = SUBJECTS[name]
        ctx = AuthzContext(subject(), core=_core(mapping))
        decision = await gen.Feature("export", of=Account.user()).decide(ctx)
        seen[name] = (decision.allowed, decision.http_status, decision.code)
    assert seen == {
        "present": (True, 200, "ok"),
        "absent.ran": (False, 501, "access_unknown"),
        "absent.failed": (False, 503, "claim_authority_failed"),
        "absent.failed.no_map": (False, 503, "claim_authority_failed"),
    }
    outdated = await gen.WithinLimit("seats", 1, of=Account.user()).decide(
        AuthzContext(_viewed("limits", view.OUTDATED), core=_core())
    )
    assert outdated.indeterminate and outdated.http_status == 401


# --------------------------------------------------------------------------- #
# Account.user() is three-valued from the token (KB-33d, G-AUTHZ-12 amended)
# --------------------------------------------------------------------------- #
ALLOW = (True, None)
DENY = (False, None)

#: The owner's table (2026-11-09): row -> (subject, mapping, atom over the
#: atom's kind, outcome). An outcome is ``ALLOW`` / ``DENY`` or the
#: indeterminate ``(status, code)``.
TOKEN: dict[str, Any] = {
    "features": {"export": True, "beta": False},
    "limits": {"seats": 3},
}
TABLE: dict[str, tuple[Any, dict[str, str] | None, dict[str, Any], Any]] = {
    "explicit.on": (
        lambda: _subject(**TOKEN),
        MAP,
        {
            "feature": lambda k: k.Feature("export", of=Account.user()),
            "limit": lambda k: k.WithinLimit("seats", 2, of=Account.user()),
        },
        ALLOW,
    ),
    "explicit.off": (
        lambda: _subject(**TOKEN),
        MAP,
        {
            "feature": lambda k: k.Feature("beta", of=Account.user()),
            "limit": lambda k: k.WithinLimit("seats", 3, of=Account.user()),
        },
        DENY,
    ),
    "complete.key_absent": (
        lambda: _subject(**TOKEN),
        MAP,
        {
            "feature": lambda k: k.Feature("nope", of=Account.user()),
            "limit": lambda k: k.WithinLimit("nope", 0, of=Account.user()),
        },
        DENY,
    ),
    "complete.empty": (
        lambda: _subject(features={}, limits={}),
        MAP,
        {
            "feature": lambda k: k.Feature("export", of=Account.user()),
            "limit": lambda k: k.WithinLimit("seats", 0, of=Account.user()),
        },
        DENY,
    ),
    "claim_absent.authority_ran": (
        lambda: _subject(),
        MAP,
        {},
        (501, "access_unknown"),
    ),
    "claim_absent.other_source_failed": (
        lambda: _subject(failed={"rogue"}),
        MAP,
        {},
        (501, "access_unknown"),
    ),
    "claim_absent.unassigned": (
        lambda: _subject(failed={"plans"}),
        UNASSIGNED_MAP,
        {},
        (501, "access_unknown"),
    ),
    "claim_absent.legacy": (
        lambda: _subject(failed=None),
        MAP,
        {},
        (501, "access_unknown"),
    ),
    "authority_failed": (
        lambda: _subject(failed={"plans"}),
        MAP,
        {},
        (503, "claim_authority_failed"),
    ),
    "authority_failed.no_map": (
        lambda: _subject(failed={"plans"}),
        None,
        {},
        (503, "claim_authority_failed"),
    ),
    "untrusted.outdated": (view.OUTDATED, MAP, {}, (401, view.OUTDATED)),
    "untrusted.mismatch": (view.MISMATCH, MAP, {}, (503, view.MISMATCH)),
    "untrusted.unassigned": (view.UNASSIGNED, MAP, {}, (501, "access_unknown")),
    "untrusted.source_absent": (view.SOURCE_ABSENT, MAP, {}, (501, "access_unknown")),
    "malformed.not_a_mapping": (
        lambda: _subject(features=["export"], limits=3),
        MAP,
        {},
        (501, "access_unknown"),
    ),
    "malformed.entry": (
        lambda: _subject(features={"export": 1}, limits={"seats": "3"}),
        MAP,
        {},
        (501, "access_unknown"),
    ),
    "malformed.entry_bool_limit": (
        lambda: _subject(features={"export": "yes"}, limits={"seats": True}),
        MAP,
        {},
        (501, "access_unknown"),
    ),
    "malformed.entry_null": (
        lambda: _subject(features={"export": None}, limits={"seats": None}),
        MAP,
        {},
        (501, "access_unknown"),
    ),
}
#: The atom a row with no atom of its own reads (``export`` / ``seats``).
DEFAULT_ATOMS: dict[str, Any] = {
    "feature": lambda k: k.Feature("export", of=Account.user()),
    "limit": lambda k: k.WithinLimit("seats", 2, of=Account.user()),
}
#: How a wrap maps the atom's outcome: ``Not`` swaps a definite answer and
#: keeps an indeterminate; an allowing sibling of ``Or`` allows on every row.
TABLE_WRAPS: dict[str, tuple[Any, Any]] = {
    "bare": (lambda a: a, lambda o: o),
    "not": (Not, lambda o: {ALLOW: DENY, DENY: ALLOW}.get(o, o)),
    "or_denied": (lambda a: Or(Role("ops"), a), lambda o: o),
    "and_allowed": (lambda a: Role("member") & a, lambda o: o),
    "not_and": (
        lambda a: Not(Role("member") & a),
        lambda o: {ALLOW: DENY, DENY: ALLOW}.get(o, o),
    ),
    "or_allowed": (lambda a: Or(Role("member"), a), lambda _o: ALLOW),
}


def _table_subject(row: str, kind: str) -> UserContext:
    subject = TABLE[row][0]
    if isinstance(subject, str):  # the view removed the atom's own claim
        return UserContext(
            user_id=uuid4(),
            roles={"member"},
            claims_sources={"idp", "plans"},
            claims_sources_failed=set(),
            **{
                view.UNTRUSTED_KEY: {
                    "features" if kind == "feature" else "limits": subject
                }
            },
        )
    built: UserContext = subject()
    return built


class _NoIO:
    """A core whose services and facts may not be asked: the user path is
    zero I/O (I-23), so any call is a failure."""

    def __init__(self, mapping: dict[str, str] | None) -> None:
        self.core = _core(mapping)
        self.asked: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.core, name)

    def get_service(self, name: str) -> Any:
        self.asked.append(name)
        raise AssertionError(f"the user path asked the contract {name!r}")

    def get_access_fact(self, name: str) -> Any:
        self.asked.append(name)
        raise AssertionError(f"the user path asked the fact {name!r}")


@pytest.mark.parametrize("wrap", sorted(TABLE_WRAPS))
@pytest.mark.parametrize("kind", ["feature", "limit"])
@pytest.mark.parametrize("row", sorted(TABLE))
async def test_the_user_path_is_three_valued_from_the_token(
    gen: Any, row: str, kind: str, wrap: str
) -> None:
    """Every row of the owner's table for Feature and WithinLimit, bare and
    under ``Not``, ``Or`` and ``And``: a present map is a complete snapshot
    (an unlisted entry denies), a missing or malformed claim is 501
    ``access_unknown``, a failed authority 503, the view's 401 / 503 kept;
    no indeterminate is ever an allow, and nothing is asked."""
    _subject_fn, mapping, atoms, outcome = TABLE[row]
    build, expect = TABLE_WRAPS[wrap]
    core = _NoIO(mapping)
    ctx = AuthzContext(_table_subject(row, kind), core=core)
    atom = atoms.get(kind, DEFAULT_ATOMS[kind])(gen)
    decision = await build(atom).decide(ctx)
    expected = expect(outcome)
    if expected == ALLOW:
        assert decision.allowed and not decision.indeterminate, decision
    elif expected == DENY:
        assert not decision.allowed and not decision.indeterminate, decision
        if wrap == "bare":
            assert decision.code == ("Feature" if kind == "feature" else "WithinLimit")
            assert decision.http_status == (403 if kind == "feature" else 402)
    else:
        status, code = expected
        assert not decision.allowed, decision
        assert decision.indeterminate, decision
        assert (decision.http_status, decision.code) == (status, code), decision
    assert core.asked == []


async def test_an_or_of_unknown_branches_never_allows(gen: Any) -> None:
    """Two branches without a source, and their negations: 501, never an allow."""
    ctx = AuthzContext(_subject(), core=_NoIO(MAP))
    feature = gen.Feature("export", of=Account.user())
    limit = gen.WithinLimit("seats", 0, of=Account.user())
    for policy in (
        Or(feature, limit),
        Or(Not(feature), Not(limit)),
        Not(feature & limit),
    ):
        decision = await policy.decide(ctx)
        assert not decision.allowed and decision.indeterminate, policy.describe()
        assert (decision.http_status, decision.code) == (501, "access_unknown")


async def test_an_unknown_names_the_claim_and_what_is_missing(gen: Any) -> None:
    absent = await gen.Feature("export", of=Account.user()).decide(
        AuthzContext(_subject(), core=_core())
    )
    assert "'features' is not in the token" in absent.reason
    assert absent.failed_requirement == "feature 'export'"
    malformed = await gen.WithinLimit("seats", 1, of=Account.user()).decide(
        AuthzContext(_subject(limits={"seats": "3"}), core=_core())
    )
    assert "entry 'seats' is not an integer" in malformed.reason
    assert malformed.failed_requirement == "within limit 'seats'"


async def test_the_user_account_is_claim_tier_and_names_no_contract(gen: Any) -> None:
    for atom in (
        gen.Feature("export", of=Account.user()),
        gen.WithinLimit("seats", 1, of=Account.user()),
    ):
        assert atom.tier == TIER_CLAIM
        assert atom.contracts() == frozenset()
    org = gen.Feature("export", of=Account.org())
    assert org.tier == TIER_LIVE
    assert org.contracts() == frozenset({PLANS_CONTRACT})
    assert org.org_params() == frozenset({"org_id"})
    assert gen.WithinLimit("seats", 1, of=Account.org()).category == (
        Category.QUOTA_EXCEEDED
    )
    with pytest.raises(ValueError, match="claim_key must be one of"):
        gen.Feature("export", of=Account.user(), claim_key="roles")


# --------------------------------------------------------------------------- #
# An organization account: the live check with the parameters
# --------------------------------------------------------------------------- #
class _Core:
    """A core serving the provider (none: the contract is not in the
    deployment) and no access fact."""

    def __init__(self, provider: Any | None) -> None:
        self.provider = provider

    def get_service(self, name: str) -> Any:
        if name != PLANS_CONTRACT or self.provider is None:
            raise ServiceUnavailableInDeployment(name)
        return self.provider

    def get_access_fact(self, name: str) -> Any:
        return None

    def remote_fact_owners(self) -> list[str]:
        return []

    def get_metric(self, name: str) -> Any:
        return None


class _Req:
    def __init__(self, org: UUID) -> None:
        self.path_params = {"org_id": str(org)}
        self.query_params: dict[str, str] = {}
        self.headers: dict[str, str] = {}
        self.scope: dict[str, Any] = {"state": {}}
        self.state = SimpleNamespace()


def _req(org: UUID) -> Any:
    """A request carrying ``org_id`` (duck-typed, as the atoms read it)."""
    return _Req(org)


ORG_ATOMS: dict[str, Any] = {
    "feature.on": lambda k: k.Feature("export", of=Account.org()),
    "feature.off": lambda k: k.Feature("beta", of=Account.org()),
    "limit.below": lambda k: k.WithinLimit("seats", 2, of=Account.org()),
    "limit.equal": lambda k: k.WithinLimit("seats", 3, of=Account.org()),
    "not.feature.off": lambda k: Not(k.Feature("beta", of=Account.org())),
}
_ALLOWED = (True, 200, None, "ok", False, None)
#: atom -> (the pinned decision, the one call: method and the parameters
#: after the account). The kernel atoms' decisions and calls on the same
#: producer (the owner's organization fact asking the same provider).
ORG_EXPECTED: dict[str, tuple[tuple[Any, ...], tuple[str, tuple[Any, ...]]]] = {
    "feature.on": (_ALLOWED, ("has_feature", ("export",))),
    "feature.off": (
        (False, 403, Category.FORBIDDEN, "Feature", False, "feature 'beta'"),
        ("has_feature", ("beta",)),
    ),
    "limit.below": (_ALLOWED, ("is_within_limit", ("seats", 2))),
    "limit.equal": (
        (
            False,
            402,
            Category.QUOTA_EXCEEDED,
            "WithinLimit",
            False,
            "within limit 'seats'",
        ),
        ("is_within_limit", ("seats", 3)),
    ),
    "not.feature.off": (_ALLOWED, ("has_feature", ("beta",))),
}


@pytest.mark.parametrize("atom", sorted(ORG_ATOMS))
async def test_an_organization_asks_the_live_check_with_its_parameters(
    gen: Any, atom: str
) -> None:
    org = uuid4()
    plans = {org: {"features": {"export": True}, "limits": {"seats": 3}}}
    provider = _Provider(plans)
    subject = UserContext(user_id=uuid4(), tenant_ids=[str(org)])
    ctx = AuthzContext(subject, request=_req(org), core=_Core(provider))
    decision = await ORG_ATOMS[atom](gen).decide(ctx)
    expected, (method, params) = ORG_EXPECTED[atom]
    assert _key(decision) == expected
    assert provider.calls == [(method, ("organization", org, *params))]


async def test_the_parameters_reach_the_provider_unchanged(gen: Any) -> None:
    org = uuid4()
    provider = _Provider({org: {"limits": {"seats": 10}}})
    subject = UserContext(user_id=uuid4(), tenant_ids=[str(org)])
    ctx = AuthzContext(subject, request=_req(org), core=_Core(provider))
    assert (await gen.WithinLimit("seats", 9, of=Account.org()).decide(ctx)).allowed
    denied = await gen.WithinLimit("seats", 10, of=Account.org()).decide(ctx)
    assert denied.category == Category.QUOTA_EXCEEDED and denied.http_status == 402
    assert denied.code == "WithinLimit"
    assert provider.calls == [
        ("is_within_limit", ("organization", org, "seats", 9)),
        ("is_within_limit", ("organization", org, "seats", 10)),
    ]


async def test_a_personal_bound_tenant_reads_the_claim(gen: Any) -> None:
    from sandloom.core.di import bind_tenant

    provider = _Provider()
    subject = _subject(**CLAIMS)
    ctx = AuthzContext(subject, request=_req(uuid4()), core=_Core(provider))
    with bind_tenant(subject.user_id):
        atom = gen.Feature("export", of=Account.bound_tenant())
        assert (await atom.decide(ctx)).allowed
    assert provider.calls == []


async def test_without_a_provider_the_organization_path_is_501(gen: Any) -> None:
    org = uuid4()
    subject = UserContext(user_id=uuid4(), tenant_ids=[str(org)])
    ctx = AuthzContext(subject, request=_req(org), core=_Core(None))
    for atom in (
        gen.Feature("export", of=Account.org()),
        gen.WithinLimit("seats", 0, of=Account.org()),
    ):
        for policy in (atom, Not(atom)):
            decision = await policy.decide(ctx)
            assert not decision.allowed
            assert decision.indeterminate and decision.http_status == 501
            assert decision.code == "service_unavailable"


# --------------------------------------------------------------------------- #
# No source for the answer: AccessUnknown, indeterminate 501 (C-B5, KB-33c)
# --------------------------------------------------------------------------- #
class _NoSource:
    """A provider that keeps no catalogue: every check is ``AccessUnknown``."""

    def __init__(self) -> None:
        self.calls = 0

    async def has_feature(self, tenant_type: str, tenant_id: UUID, key: str) -> bool:
        self.calls += 1
        raise AccessUnknown(f"no feature catalogue (feature {key!r})")

    async def is_within_limit(
        self, tenant_type: str, tenant_id: UUID, key: str, used: int
    ) -> bool:
        self.calls += 1
        raise AccessUnknown(f"no limits catalogue (limit {key!r})")


#: Policies over an organization's check with no source: none may allow.
NO_SOURCE: dict[str, Any] = {
    "feature": lambda k: k.Feature("beta", of=Account.org()),
    "limit": lambda k: k.WithinLimit("seats", 3, of=Account.org()),
    "not.feature": lambda k: Not(k.Feature("beta", of=Account.org())),
    "not.limit": lambda k: Not(k.WithinLimit("seats", 3, of=Account.org())),
    "or.both": lambda k: Or(
        k.Feature("beta", of=Account.org()),
        k.WithinLimit("seats", 3, of=Account.org()),
    ),
    "or.denied": lambda k: Or(Role("ops"), k.Feature("beta", of=Account.org())),
    "or.not": lambda k: Or(Role("ops"), Not(k.Feature("beta", of=Account.org()))),
}


def _assert_unknown(decision: Any) -> None:
    assert not decision.allowed
    assert decision.indeterminate and decision.http_status == 501
    assert decision.code == "access_unknown"
    assert decision.category == Category.POLICY_MISCONFIGURED


@pytest.mark.parametrize("policy", sorted(NO_SOURCE))
async def test_no_source_is_501_and_never_allows(gen: Any, policy: str) -> None:
    """A provider that cannot answer is never a deny ``Not`` turns into an
    allow: indeterminate 501 ``access_unknown``, its words in the reason."""
    org = uuid4()
    provider = _NoSource()
    subject = UserContext(user_id=uuid4(), roles={"member"}, tenant_ids=[str(org)])
    ctx = AuthzContext(subject, request=_req(org), core=_Core(provider))
    decision = await NO_SOURCE[policy](gen).decide(ctx)
    _assert_unknown(decision)
    assert "catalogue" in decision.reason
    assert PLANS_CONTRACT in decision.reason
    assert provider.calls >= 1


async def test_no_source_still_lets_a_reliable_sibling_allow(gen: Any) -> None:
    """``Or`` allows on another branch that reliably allowed (the algebra's
    indeterminate rule), and the claims path never asks the provider."""
    org = uuid4()
    provider = _NoSource()
    subject = _subject(tenant_ids=[str(org)], **CLAIMS)
    ctx = AuthzContext(subject, request=_req(org), core=_Core(provider))
    atom = gen.Feature("beta", of=Account.org())
    assert (await Or(Role("member"), atom).decide(ctx)).allowed
    assert (await gen.Feature("export", of=Account.user()).decide(ctx)).allowed
    denied = await gen.Feature("beta", of=Account.user()).decide(ctx)
    assert not denied.allowed and denied.http_status == 403
    assert provider.calls == 0  # Or decided on its cheaper branch first


# --------------------------------------------------------------------------- #
# A third-party source (no battery), mono and split over the real internal RPC
# --------------------------------------------------------------------------- #
ACME_CONTRACT = "acme.plans.v1"
ACME_SVC = "acme"
ACME_ORG = UUID("00000000-0000-4000-8000-0000000ac0e1")
ACME_NONE = UUID("00000000-0000-4000-8000-0000000ac0e2")
#: The third party's plan catalogue, by organization.
ACME_CATALOGUE: dict[UUID, dict[str, Any]] = {
    ACME_ORG: {"features": {"export": True, "beta": False}, "limits": {"seats": 3}}
}


@contract(ACME_CONTRACT)
class AcmePlansContract:
    """A third-party plan catalogue: an organization it has no plan for is
    unknown, a key its plan does not list is off (limit 0)."""

    @claim_atom("Feature", claim_key="features", entry="key")
    @access_check(
        "LiveFeature",
        bind={"tenant_type": From.account_type(), "tenant_id": From.account_id()},
        describe="feature '{key}'",
    )
    async def has_feature(self, tenant_type: str, tenant_id: UUID, key: str) -> bool:
        """True when the plan enables ``key``; unknown without a plan."""
        plan = ACME_CATALOGUE.get(tenant_id)
        if plan is None:
            raise AccessUnknown(f"acme has no plan for {tenant_id}")
        return plan["features"].get(key) is True

    @claim_atom("WithinLimit", claim_key="limits", entry="key", below="used")
    @access_check(
        "LiveWithinLimit",
        bind={"tenant_type": From.account_type(), "tenant_id": From.account_id()},
        category=Category.QUOTA_EXCEEDED,
        describe="within limit '{key}'",
    )
    async def is_within_limit(
        self, tenant_type: str, tenant_id: UUID, key: str, used: int
    ) -> bool:
        """True when ``used`` is below the plan limit; unknown without a plan."""
        plan = ACME_CATALOGUE.get(tenant_id)
        if plan is None:
            raise AccessUnknown(f"acme has no plan for {tenant_id}")
        limit: int = plan["limits"].get(key, 0)
        return used < limit


class _UoW:
    async def __aenter__(self) -> _UoW:
        self.session = SimpleNamespace(info={})
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None


class _Database:
    def uow_factory(self) -> Any:
        return _UoW

    def outbox(self) -> None:
        return None

    def audit(self) -> None:
        return None


@pytest.fixture
def acme(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """The third-party provider registered on a core keyed for internal RPC,
    and the atoms ``sandloom generate`` writes beside its stub."""
    from sandloom.core.config.settings import settings

    allowed = {ACME_SVC: [ACME_CONTRACT]}
    for name, value in rpc_settings(ACME_SVC, allowed_calls=allowed).items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(idempotency_cache_store, "cache", lambda *_a, **_k: _MEM)
    core = CoreFacade(app=FastAPI())
    core.database = lambda name="default": _Database()  # type: ignore[method-assign]
    core.register_service(ACME_CONTRACT, AcmePlansContract)
    module = _generated(tmp_path, ACME_CONTRACT, AcmePlansContract, "_kb33c_acme")
    yield SimpleNamespace(core=core, gen=module)
    sys.modules.pop(module.__name__, None)


_MEM = InMemoryCache()

#: (policy, organization) -> (allowed, status, code) in both topologies.
ACME_CASES: dict[str, tuple[Any, UUID, tuple[bool, int, str]]] = {
    "feature.on": (
        lambda k: k.Feature("export", of=Account.org()),
        ACME_ORG,
        (True, 200, "ok"),
    ),
    "feature.off": (
        lambda k: k.Feature("beta", of=Account.org()),
        ACME_ORG,
        (False, 403, "Feature"),
    ),
    "not.feature.off": (
        lambda k: Not(k.Feature("beta", of=Account.org())),
        ACME_ORG,
        (True, 200, "ok"),
    ),
    "limit.below": (
        lambda k: k.WithinLimit("seats", 2, of=Account.org()),
        ACME_ORG,
        (True, 200, "ok"),
    ),
    "limit.equal": (
        lambda k: k.WithinLimit("seats", 3, of=Account.org()),
        ACME_ORG,
        (False, 402, "WithinLimit"),
    ),
    "not.limit.equal": (
        lambda k: Not(k.WithinLimit("seats", 3, of=Account.org())),
        ACME_ORG,
        (True, 200, "ok"),
    ),
    "unknown.feature": (
        lambda k: k.Feature("export", of=Account.org()),
        ACME_NONE,
        (False, 501, "access_unknown"),
    ),
    "unknown.not.feature": (
        lambda k: Not(k.Feature("export", of=Account.org())),
        ACME_NONE,
        (False, 501, "access_unknown"),
    ),
    "unknown.not.limit": (
        lambda k: Not(k.WithinLimit("seats", 0, of=Account.org())),
        ACME_NONE,
        (False, 501, "access_unknown"),
    ),
    "unknown.or": (
        lambda k: Or(
            k.Feature("export", of=Account.org()),
            k.WithinLimit("seats", 0, of=Account.org()),
        ),
        ACME_NONE,
        (False, 501, "access_unknown"),
    ),
}


class _Serving(_Core):
    """A core serving one provider (local, or the RPC proxy) for the contract."""

    def get_service(self, name: str) -> Any:
        if name != ACME_CONTRACT:
            raise ServiceUnavailableInDeployment(name)
        return self.provider


@pytest.mark.parametrize("case", sorted(ACME_CASES))
async def test_a_third_party_source_decides_alike_mono_and_split(
    acme: Any, case: str
) -> None:
    """``acme.plans.v1`` (not billing) implements Feature / WithinLimit through
    its own generated atoms and contract, no kernel change: the same
    decisions in-process and over the real internal RPC (signed call,
    dispatch, error envelope), ``AccessUnknown`` reaching the remote
    caller as the same 501."""
    policy, org, expected = ACME_CASES[case]
    subject = UserContext(user_id=uuid4(), tenant_ids=[str(org)])
    async with in_process_rpc_client(acme.core) as client:
        topologies = {
            "mono": acme.core.get_service(ACME_CONTRACT),
            "split": rpc_proxy_for(acme.core, ACME_CONTRACT, client),
        }
        outcomes = {}
        for name, provider in topologies.items():
            core = _Serving(provider)
            ctx = AuthzContext(subject, request=_req(org), core=core)
            decision = await policy(acme.gen).decide(ctx)
            outcomes[name] = _key(decision)
    assert outcomes["split"] == outcomes["mono"]
    assert outcomes["mono"][:2] == expected[:2]
    assert outcomes["mono"][3] == expected[2]
    assert outcomes["mono"][4] is (expected[2] == "access_unknown")


async def test_access_unknown_is_the_same_class_local_and_remote(acme: Any) -> None:
    """A direct caller gets the provider's 501 ``AccessUnknown`` either way."""
    local = acme.core.get_service(ACME_CONTRACT)
    with pytest.raises(AccessUnknown) as local_exc:
        await local.has_feature("organization", ACME_NONE, "export")
    async with in_process_rpc_client(acme.core) as client:
        remote = rpc_proxy_for(acme.core, ACME_CONTRACT, client)
        with pytest.raises(AccessUnknown) as remote_exc:
            await remote.has_feature("organization", ACME_NONE, "export")
    assert local_exc.value.status_code == remote_exc.value.status_code == 501
    assert local_exc.value.detail == remote_exc.value.detail


class _DownProvider:
    """A bound provider that is down (K5): every call fails over RPC."""

    def __init__(self) -> None:
        self.calls = 0

    async def has_feature(self, *_args: Any) -> bool:
        self.calls += 1
        raise RemoteServiceUnavailable(
            PLANS_CONTRACT, reason="unreachable", code="remote_service_unavailable"
        )


async def test_a_provider_down_is_503_and_never_allows(gen: Any) -> None:
    """The organization path inherits K5: indeterminate 503, ``Not`` never
    allows, ``Or`` still allows on a genuine allow; the claims path never
    asks the provider at all."""
    org = uuid4()
    down = _DownProvider()
    subject = _subject(tenant_ids=[str(org)], **CLAIMS)
    ctx = AuthzContext(subject, request=_req(org), core=_Core(down))
    atom = gen.Feature("export", of=Account.org())
    for policy in (atom, Not(atom)):
        decision = await policy.decide(ctx)
        assert not decision.allowed
        assert decision.indeterminate and decision.http_status == 503
    assert (
        await Or(atom, gen.Feature("export", of=Account.user())).decide(ctx)
    ).allowed
    assert down.calls == 2  # the Or decided on its cheaper, claims branch


async def test_no_request_session_is_opened(gen: Any) -> None:
    """K6: the claim is in the token and the live check runs in the
    provider's unit, so ``require()`` opens no session for either."""
    for atom in (
        gen.Feature("export", of=Account.user()),
        gen.WithinLimit("seats", 1, of=Account.org()),
        gen.Feature("export", of=Account.bound_tenant()),
    ):
        for policy in (atom, Not(atom), atom & Role("admin")):
            assert not policy.needs_request_session(), policy.describe()


# --------------------------------------------------------------------------- #
# Compatibility (SANDLOOM-T004)
# --------------------------------------------------------------------------- #
async def test_changing_a_claim_read_is_incompatible() -> None:
    before = contract_schema(PlansContract).describe()
    for field, value in (("read", "flag"), ("entry", "used"), ("below", None)):
        after = copy.deepcopy(before)
        after.pop("hash", None)  # the provider's new document
        claim = after["methods"]["is_within_limit"]["access_check"]["claim"]
        claim[field] = value
        report = compare_contract(PLANS_CONTRACT, before, after)
        assert any(f"claim atom {field} changed" in e for e in report.errors), field
    assert not compare_contract(PLANS_CONTRACT, before, before).errors
