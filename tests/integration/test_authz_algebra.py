"""Unit tests for the shared authorization algebra (no DB, no FastAPI).

Exercises the pure predicate layer directly via AuthzContext.evaluate, including
tier ordering / lazy resource loading and the inline `authorize` helper. The
billing atoms are the generated ones (``generated_atoms``, KB-34).
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from sandloom.shared.authz import (
    AccessFact,
    Account,
    Owner,
    Perm,
    Role,
    authorize,
    require,
)
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.authz.facts import ClaimRule
from sandloom.shared.subject import UserContext

pytestmark = pytest.mark.asyncio

USER_ID = uuid4()


def _subject(**overrides: object) -> UserContext:
    data: dict[str, object] = {
        "user_id": USER_ID,
        "roles": set(),
        "permissions": set(),
        "subscription_status": None,
        "features": {},
        "limits": {},
    }
    data.update(overrides)
    return UserContext(**data)  # type: ignore[arg-type]


class _Core:
    """Billing's ``subscription.active`` fact registered: the generated
    ``Subscribed`` reads the claim under it (501 with no owner)."""

    def get_access_fact(self, name: str) -> AccessFact | None:
        return SUBSCRIPTION if name == SUBSCRIPTION.name else None


SUBSCRIPTION = AccessFact(
    name="subscription.active",
    owner_module="billing",
    claim_key="subscription_status",
    claim_rule=ClaimRule.one_of({"active", "trialing"}),
)


def _ctx(subject: UserContext, **kw: object) -> AuthzContext:
    kw.setdefault("core", _Core())
    return AuthzContext(subject, **kw)  # type: ignore[arg-type]


# -- claim atoms ---------------------------------------------------------------


async def test_perm_and_role_membership() -> None:
    s = _subject(roles={"admin"}, permissions={"post.write"})
    assert await Perm("post.write").evaluate(_ctx(s)) is True
    assert await Perm("post.delete").evaluate(_ctx(s)) is False
    assert await Role("admin").evaluate(_ctx(s)) is True
    assert await Role("ops").evaluate(_ctx(s)) is False


async def test_subscribed_feature_limit(generated_atoms: SimpleNamespace) -> None:
    atoms = generated_atoms
    Subscribed, Feature, WithinLimit = (
        atoms.Subscribed,
        atoms.Feature,
        atoms.WithinLimit,
    )
    s = _subject(
        subscription_status="trialing", features={"export": True}, limits={"seats": 3}
    )
    assert await Subscribed(of=Account.user()).evaluate(_ctx(s)) is True
    assert (
        await Subscribed(of=Account.user()).evaluate(
            _ctx(_subject(subscription_status="canceled"))
        )
        is False
    )
    assert await Feature("export", of=Account.user()).evaluate(_ctx(s)) is True
    assert await Feature("missing", of=Account.user()).evaluate(_ctx(s)) is False
    assert (
        await WithinLimit("seats", used=2, of=Account.user()).evaluate(_ctx(s)) is True
    )
    assert (
        await WithinLimit("seats", used=3, of=Account.user()).evaluate(_ctx(s)) is False
    )
    # an entry the (complete) limits mapping does not list fails closed
    assert (
        await WithinLimit("unknown", used=0, of=Account.user()).evaluate(_ctx(s))
        is False
    )


# -- combinators ---------------------------------------------------------------


async def test_and_or_not(generated_atoms: SimpleNamespace) -> None:
    Subscribed = generated_atoms.Subscribed
    s = _subject(
        roles={"admin"}, permissions={"post.write"}, subscription_status="active"
    )
    assert (
        await (Perm("post.write") & Subscribed(of=Account.user())).evaluate(_ctx(s))
        is True
    )
    assert await (Perm("post.write") & Perm("post.delete")).evaluate(_ctx(s)) is False
    assert await (Perm("post.delete") | Role("admin")).evaluate(_ctx(s)) is True
    assert await (~Perm("post.delete")).evaluate(_ctx(s)) is True
    assert await (~Role("admin")).evaluate(_ctx(s)) is False


# -- tier ordering / lazy resource loading ------------------------------------


async def test_cheap_claim_short_circuits_before_resource_load() -> None:
    """A failing claim in an AND must prevent the resource loader from running."""
    calls = {"n": 0}

    async def loader(_ctx: AuthzContext) -> object:
        calls["n"] += 1
        return object()

    s = _subject(permissions=set())  # lacks the permission
    policy = Perm("post.write") & Owner(lambda r: USER_ID)
    ctx = _ctx(s, loader=loader)
    assert await policy.evaluate(ctx) is False
    assert calls["n"] == 0  # resource never loaded — claim decided first


async def test_owner_loads_resource_once() -> None:
    calls = {"n": 0}

    class Post:
        author_id = USER_ID

    async def loader(_ctx: AuthzContext) -> Post:
        calls["n"] += 1
        return Post()

    s = _subject(permissions={"post.write"})
    policy = Perm("post.write") & Owner(lambda p: p.author_id)
    ctx = _ctx(s, loader=loader)
    assert await policy.evaluate(ctx) is True
    # second access returns the cached resource, no extra load
    await ctx.get_resource()
    assert calls["n"] == 1


async def test_owner_rejects_non_owner() -> None:
    class Post:
        author_id = uuid4()  # someone else

    s = _subject()
    ctx = _ctx(s, resource=Post())
    assert await Owner(lambda p: p.author_id).evaluate(ctx) is False


# -- inline authorize ----------------------------------------------------------


async def test_authorize_inline_raises_403() -> None:
    class Post:
        author_id = uuid4()

    s = _subject(roles=set())
    with pytest.raises(HTTPException) as exc:
        await authorize(Post(), Owner(lambda p: p.author_id) | Role("admin"), ctx=s)
    assert exc.value.status_code == 403


async def test_authorize_inline_passes_for_owner() -> None:
    class Post:
        author_id = USER_ID

    s = _subject()
    # no raise
    await authorize(Post(), Owner(lambda p: p.author_id), ctx=s)


# -- require() rejects resource-scoped policies at wiring time -----------------


async def test_require_rejects_resource_policy() -> None:
    # Owner has no resource under require(); must fail loudly at wiring time
    # (not as a runtime 500), directly or nested in a combinator.
    with pytest.raises(ValueError):
        require(Owner(lambda p: p.owner_id))
    with pytest.raises(ValueError):
        require(Owner(lambda p: p.owner_id) | Role("admin"))


async def test_require_allows_claim_and_live_policies(
    generated_atoms: SimpleNamespace,
) -> None:
    # Claim and live (Credits) atoms need no resource -> require() accepts them.
    atoms = generated_atoms
    assert callable(require(Perm("x")))
    assert callable(require(atoms.Credits(10, of=Account.user())))
    assert callable(require(atoms.Subscribed(of=Account.user()) & Role("admin")))
