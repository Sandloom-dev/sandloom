"""The mint behind ``sandloom dev token`` (``sandloom.core.auth.dev_token``).

The token must be what the app's own verifier accepts, so most rows send it
through the real gates of the claim-policy matrix (``Role``, ``TenantScope``;
``test_claims_split_matrix``) on a verifier composed with a real
``claim_authorities`` map. The refusals are checked against the loaded settings
(``ENVIRONMENT``), the policy (a claim nobody is assigned) and the bounds.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import jwt
import pytest

from sandloom.core.auth import dev_token as dt
from sandloom.core.auth import tokens
from sandloom.core.claims import NO_AUTHORITIES, ClaimAuthorities
from sandloom.core.config.common import Environment
from sandloom.core.config.settings import settings
from sandloom.core.tests import test_auth_rotation_revocation as _revocation
from sandloom.core.tests._entry_points_support import installed_entry_points
from sandloom.core.tests.test_claims_split_matrix import (
    ACME,
    ID,
    OK,
    ROLES_401,
    roles_map,
    statuses,
    verifier,
)

mem_cache = _revocation.mem_cache

NO_ROLE = {"/role": 403, "/perm": 403, "/admin": 403, "/operator": 403}


@pytest.fixture(autouse=True)
def _standard_entry_points() -> Iterator[None]:
    with installed_entry_points():
        yield


@pytest.fixture(autouse=True)
def _fresh_verification_cache() -> Iterator[None]:
    tokens._verified_tokens.clear()
    yield
    tokens._verified_tokens.clear()


def _mint(policy: ClaimAuthorities, **kwargs: Any) -> dt.DevToken:
    kwargs.setdefault("user_id", uuid4())
    return dt.mint_dev_token(policy, **kwargs)


def _claims(minted: dt.DevToken) -> dict[str, Any]:
    """What the token states, before any verifier's policy trims it (the
    verified read, ``decode_token``, drops the claims its policy does not
    trust, and here no active core holds one)."""
    assert settings.jwt_secret
    return jwt.decode(
        minted.token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        options={"verify_aud": False},
    )


# --- the token is what the app's verifier accepts ------------------------------
@pytest.mark.asyncio
async def test_the_token_gets_exactly_the_rights_it_asks_for(mem_cache: Any) -> None:
    core = verifier(roles_map(ID))
    policy = core.claim_authorities
    tenant = uuid4()

    both = _mint(policy, tenant_id=tenant, roles=["admin"])
    # Role and tenant gates open; no permission was asked for, none is given.
    assert await statuses(core, both.token) == {**OK, "/perm": 403}

    roles_only = await statuses(core, _mint(policy, roles=["admin"]).token)
    assert roles_only["/role"] == 200
    assert roles_only["/tenant"] != 200

    tenant_only = await statuses(core, _mint(policy, tenant_id=tenant).token)
    assert tenant_only == {**NO_ROLE, "/tenant": 200}

    # Neither flag: a valid subject with no rights (a route needing only a
    # login would accept it).
    plain = await statuses(core, _mint(policy).token)
    assert {gate: plain[gate] for gate in NO_ROLE} == NO_ROLE
    assert plain["/tenant"] != 200


@pytest.mark.asyncio
async def test_the_token_carries_the_provenance_of_the_assigned_authority(
    mem_cache: Any,
) -> None:
    """The digests and sources come from the policy it was given: the same
    token read by a verifier whose roles authority is another module is an
    outdated token (401, one refresh), not a trusted one."""
    minted = _mint(
        verifier(roles_map(ID)).claim_authorities, tenant_id=uuid4(), roles=["admin"]
    )
    # The roles gates (401, one refresh), not 200; `/perm` stays a plain 403
    # because no permissions claim was asked for.
    assert await statuses(verifier(roles_map(ACME)), minted.token) == {
        **ROLES_401,
        "/perm": 403,
    }


def test_the_claims_are_what_the_policy_asks_for() -> None:
    policy = verifier(roles_map(ID)).claim_authorities
    user, tenant = uuid4(), uuid4()
    minted = _mint(
        policy,
        user_id=user,
        tenant_id=tenant,
        roles=["viewer", "admin", "viewer"],
        ttl_minutes=30,
    )
    claims = _claims(minted)
    assert claims["sub"] == str(user)
    assert claims["token_type"] == "access"
    assert claims["roles"] == ["admin", "viewer"]  # sorted, unique
    assert claims["tenant_ids"] == [str(tenant)]
    assert claims["tenant_id"] == str(tenant)
    assert "permissions" not in claims
    assert claims["claims_sources"] == ["grants", "identity"]
    assert claims["claims_sources_failed"] == []
    assert set(claims["cpv"]["k"]) == set(policy.accepted)
    assert claims["exp"] - claims["iat"] == 30 * 60
    assert (minted.user_id, minted.tenant_id) == (user, tenant)
    assert minted.roles == ("admin", "viewer")
    assert minted.authorities == ("grants", "identity")


def test_no_flag_claims_nothing_and_needs_no_authority() -> None:
    minted = _mint(NO_AUTHORITIES)
    claims = _claims(minted)
    assert claims["claims_sources"] == []
    assert "roles" not in claims
    assert "tenant_ids" not in claims
    assert claims["exp"] - claims["iat"] == dt.DEFAULT_TTL_MINUTES * 60


# --- a claim nobody is assigned is refused, never silently dropped ------------
@pytest.mark.parametrize(
    ("kwargs", "flag"),
    [
        ({"roles": ["admin"]}, "--role"),
        ({"tenant_id": uuid4()}, "--tenant"),
        ({"roles": ["admin"], "tenant_id": uuid4()}, "--role"),
    ],
)
def test_a_claim_without_an_assigned_authority_is_refused(
    kwargs: dict[str, Any], flag: str
) -> None:
    with pytest.raises(dt.DevTokenRefused, match="claim_authorities") as refused:
        _mint(NO_AUTHORITIES, **kwargs)
    assert flag in str(refused.value)


def test_each_claim_needs_its_own_authority() -> None:
    mapping = roles_map(ID)
    del mapping["tenant_ids"]
    policy = verifier(mapping).claim_authorities
    assert _claims(_mint(policy, roles=["admin"]))["roles"] == ["admin"]
    with pytest.raises(dt.DevTokenRefused, match="--tenant"):
        _mint(policy, tenant_id=uuid4())


# --- outside dev / test nothing is minted -------------------------------------
def test_the_dev_environments_are_an_allow_list() -> None:
    assert dt.DEV_ENVIRONMENTS == ("dev", "test")
    assert {env.value for env in Environment} >= set(dt.DEV_ENVIRONMENTS)


@pytest.mark.parametrize("env", [Environment.dev, Environment.test])
def test_it_mints_in_dev_and_test(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "environment", env)
    assert _mint(NO_AUTHORITIES).token


@pytest.mark.parametrize(
    "env", [env for env in Environment if env.value not in dt.DEV_ENVIRONMENTS]
)
def test_it_refuses_every_other_environment(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "environment", env)
    with pytest.raises(dt.DevTokenRefused, match=f"ENVIRONMENT={env.value}"):
        _mint(NO_AUTHORITIES)


def test_the_environment_is_checked_before_the_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A production process learns nothing about its claim policy from the
    refusal: the environment answer comes first."""
    monkeypatch.setattr(settings, "environment", Environment.prod)
    with pytest.raises(dt.DevTokenRefused, match="ENVIRONMENT=prod"):
        _mint(NO_AUTHORITIES, roles=["admin"], tenant_id=uuid4())


# --- bounds ---------------------------------------------------------------------
@pytest.mark.parametrize("ttl", [0, -1, dt.MAX_TTL_MINUTES + 1, 10**9])
def test_a_ttl_out_of_bounds_is_refused(ttl: int) -> None:
    with pytest.raises(dt.DevTokenRefused, match="minutes"):
        _mint(NO_AUTHORITIES, ttl_minutes=ttl)


@pytest.mark.parametrize("ttl", [1, dt.MAX_TTL_MINUTES])
def test_the_ttl_bounds_are_inclusive(ttl: int) -> None:
    claims = _claims(_mint(NO_AUTHORITIES, ttl_minutes=ttl))
    assert claims["exp"] - claims["iat"] == ttl * 60


@pytest.mark.parametrize(
    "role",
    ["", " admin", "admin ", "a b", "ad\nmin", "-admin", "x" * 65, "rôle", "a/b"],
)
def test_a_role_that_is_not_a_role_name_is_refused(role: str) -> None:
    policy = verifier(roles_map(ID)).claim_authorities
    with pytest.raises(dt.DevTokenRefused, match="not a role name"):
        _mint(policy, roles=["admin", role])


def test_role_names_the_pattern_allows() -> None:
    policy = verifier(roles_map(ID)).claim_authorities
    names = ["admin", "a", "x" * 64, "billing.admin", "org:owner", "r_1-b"]
    assert _claims(_mint(policy, roles=names))["roles"] == sorted(names)


def test_too_many_roles_are_refused() -> None:
    policy = verifier(roles_map(ID)).claim_authorities
    at_limit = [f"r{i}" for i in range(dt.MAX_ROLES)]
    assert len(_claims(_mint(policy, roles=at_limit))["roles"]) == dt.MAX_ROLES
    with pytest.raises(dt.DevTokenRefused, match="at most"):
        _mint(policy, roles=[*at_limit, "one-more"])


# --- no secret anywhere it is not needed ----------------------------------------
def test_no_output_names_the_signing_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = settings.jwt_secret
    assert secret
    policy = verifier(roles_map(ID)).claim_authorities
    minted = _mint(policy, tenant_id=uuid4(), roles=["admin"])
    messages = [repr(minted), minted.token]
    # Every refusal text.
    for call in (
        lambda: _mint(NO_AUTHORITIES, roles=["admin"]),
        lambda: _mint(NO_AUTHORITIES, tenant_id=uuid4()),
        lambda: _mint(policy, ttl_minutes=0),
        lambda: _mint(policy, roles=["a b"]),
    ):
        with pytest.raises(dt.DevTokenRefused) as refused:
            call()
        messages.append(str(refused.value))
    monkeypatch.setattr(settings, "environment", Environment.prod)
    with pytest.raises(dt.DevTokenRefused) as refused:
        _mint(policy)
    messages.append(str(refused.value))
    assert len(messages) == 7
    assert [m for m in messages if secret in m] == []


def test_the_token_is_not_in_the_repr() -> None:
    minted = _mint(NO_AUTHORITIES)
    assert minted.token not in repr(minted)
