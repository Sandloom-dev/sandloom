"""The mint behind ``sandloom dev token`` (``sandloom.core.auth.dev_token``).

The token must be what the app's own verifier accepts, so most rows send it
through the real gates of the claim-policy matrix (``Role``, ``TenantScope``;
``test_claims_split_matrix``) on a verifier composed with a real
``claim_authorities`` map. The refusals are checked against the loaded settings
(``ENVIRONMENT``), the policy (a claim nobody is assigned) and the bounds.

The last section is the verifier's side of the sealed ``dev_mint`` marker:
a verifier that is not running dev or test refuses every token that carries it,
which is what keeps a production app from accepting a token minted with its key
on a developer machine.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import jwt
import pytest

from sandloom.core.auth import dev_token as dt
from sandloom.core.auth import tokens
from sandloom.core.claims import (
    DEV_MINT_CLAIM,
    NO_AUTHORITIES,
    ClaimAuthorities,
    UnsealedClaimsError,
)
from sandloom.core.config.common import Environment
from sandloom.core.config.settings import settings
from sandloom.core.tests import test_auth_rotation_revocation as _revocation
from sandloom.core.tests._entry_points_support import installed_entry_points
from sandloom.core.tests.test_claims_split_matrix import (
    ACME,
    ALL_401,
    GATED,
    ID,
    OK,
    ROLES_401,
    roles_map,
    statuses,
    verifier,
)
from sandloom.core.tests.test_environment_unset import process_environment

mem_cache = _revocation.mem_cache

NO_ROLE = {"/role": 403, "/perm": 403, "/admin": 403, "/operator": 403}


@pytest.fixture(autouse=True)
def _standard_entry_points() -> Iterator[None]:
    with installed_entry_points():
        yield


@pytest.fixture(autouse=True)
def _explicit_dev_environment() -> Iterator[None]:
    """The rows run as an app with ``ENVIRONMENT=dev`` set: the framework suite
    leaves it unset, and an unset ``ENVIRONMENT`` accepts no dev token (the
    ``unset`` rows below set that state themselves)."""
    with process_environment(Environment.dev):
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
async def test_a_flagless_token_reaches_a_tenant_route_only_in_single_tenant_mode(
    mem_cache: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An app without the tenancy battery (``sandloom new --no-batteries``):
    no authority assigns tenants, so a token without flags binds none. Under
    the default ``TENANCY_MODE=multi`` the tenant route refuses it (400); the
    ``single`` mode the scaffold's ``.env`` writes binds the canonical tenant
    and the same token reaches the route (200)."""
    core = verifier({})
    # the tenant gate reads the mode from the settings of the app's core
    monkeypatch.setattr(GATED.state, "core", core, raising=False)
    token = _mint(core.claim_authorities).token
    assert (await statuses(core, token))["/tenant"] == 400
    canonical = uuid4()
    core.settings = settings.model_copy(
        update={"tenancy_mode": "single", "single_tenant_id": canonical}
    )
    assert (await statuses(core, token))["/tenant"] == 200


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


def test_it_refuses_to_mint_while_environment_is_unset() -> None:
    """Unset reads ``dev``, but is not a development machine: the verifier
    refuses what this would mint, so it does not mint."""
    with process_environment(None):
        assert settings.is_dev  # the value is the default one
        with pytest.raises(dt.DevTokenRefused, match="ENVIRONMENT is not set"):
            _mint(NO_AUTHORITIES)
        with pytest.raises(dt.DevTokenRefused, match="ENVIRONMENT is not set"):
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


# --- the sealed marker: a verifier outside dev and test refuses a dev token -------
NON_DEV = [env for env in Environment if env.value not in dt.DEV_ENVIRONMENTS]


def _marked(value: Any) -> str:
    """A token signed with the app's key whose only dev-token trait is the
    marker (with ``value``): only the marker can decide what a verifier does."""
    claims = {"sub": str(uuid4()), "token_type": "access", DEV_MINT_CLAIM: value}
    return tokens._mint(claims, None, None)


def test_the_verifier_and_the_mint_share_the_dev_environments() -> None:
    assert dt.DEV_ENVIRONMENTS is tokens.DEV_ENVIRONMENTS
    assert tokens.DEV_ENVIRONMENTS == ("dev", "test")
    assert Environment.prod in NON_DEV  # the refusal rows below are not empty


def test_every_dev_token_carries_the_marker() -> None:
    policy = verifier(roles_map(ID)).claim_authorities
    for minted in (
        _mint(NO_AUTHORITIES),
        _mint(policy, roles=["admin"]),
        _mint(policy, tenant_id=uuid4(), roles=["admin"], ttl_minutes=1),
    ):
        assert _claims(minted)[DEV_MINT_CLAIM] is True


@pytest.mark.parametrize(
    "mint", [tokens.create_access_token, tokens.create_service_token]
)
def test_no_mint_takes_the_marker_from_its_caller(mint: Any) -> None:
    """Sealed like ``cpv``: only the dev token's sealed result writes it, so a
    verifier's refusal is never triggered (or dodged) through ``data=``."""
    with pytest.raises(UnsealedClaimsError, match=DEV_MINT_CLAIM):
        mint({"sub": str(uuid4()), "token_type": "access", DEV_MINT_CLAIM: True})


@pytest.mark.parametrize("env", [Environment.dev, Environment.test])
def test_a_dev_or_test_verifier_accepts_a_dev_token(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    minted = _mint(NO_AUTHORITIES)
    monkeypatch.setattr(settings, "environment", env)
    claims = tokens.decode_token(minted.token)
    assert claims is not None
    assert claims["sub"] == str(minted.user_id)
    assert claims[DEV_MINT_CLAIM] is True


@pytest.mark.parametrize("env", NON_DEV)
def test_a_verifier_in_any_other_environment_refuses_a_dev_token(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Minted where a dev token may be, presented to a verifier that holds the
    same key but runs as another environment: a copied production ``.env``."""
    minted = _mint(NO_AUTHORITIES)
    monkeypatch.setattr(settings, "environment", env)
    assert tokens.decode_token(minted.token) is None


@pytest.mark.asyncio
async def test_a_production_app_answers_401_at_every_gate_whatever_the_token_names(
    mem_cache: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = verifier(roles_map(ID))
    token = _mint(core.claim_authorities, tenant_id=uuid4(), roles=["admin"]).token
    assert await statuses(core, token) == {**OK, "/perm": 403}  # dev: it works
    monkeypatch.setattr(settings, "environment", Environment.prod)
    # prod: the same token is nobody. The role gates answer 401; `TenantScope`
    # binds no tenant for it (400 tenant_required, as for no token at all).
    assert await statuses(core, token) == {**ALL_401, "/tenant": 400}


def test_the_verification_cache_is_keyed_on_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token verified (and cached) under dev is not served from the cache to
    a verifier that has since become prod, and is accepted again under dev."""
    minted = _mint(NO_AUTHORITIES)
    assert tokens.decode_token(minted.token) is not None
    assert tokens.decode_token(minted.token) is not None  # the cached answer
    monkeypatch.setattr(settings, "environment", Environment.prod)
    assert tokens.decode_token(minted.token) is None
    monkeypatch.setattr(settings, "environment", Environment.dev)
    assert tokens.decode_token(minted.token) is not None


def test_a_verifier_with_environment_unset_refuses_a_dev_token() -> None:
    """A deploy that forgot ``ENVIRONMENT`` reads ``dev``, and holds the key a
    developer machine signed with: the token is nobody there, and accepted
    again once ``ENVIRONMENT=dev`` is set."""
    minted = _mint(NO_AUTHORITIES)
    assert tokens.decode_token(minted.token) is not None
    with process_environment(None):
        assert settings.is_dev
        assert tokens.decode_token(minted.token) is None
    assert tokens.decode_token(minted.token) is not None


def test_the_verification_cache_is_keyed_on_whether_the_environment_was_set() -> None:
    """A token verified (and cached) with ``ENVIRONMENT=dev`` set is not served
    from the cache to the same process once it reads the default."""
    minted = _mint(NO_AUTHORITIES)
    assert tokens.decode_token(minted.token) is not None
    assert tokens.decode_token(minted.token) is not None  # the cached answer
    with process_environment(None):
        assert tokens.decode_token(minted.token) is None
        assert tokens.decode_token(minted.token) is None


@pytest.mark.asyncio
async def test_an_app_with_environment_unset_answers_401_at_every_gate(
    mem_cache: Any,
) -> None:
    core = verifier(roles_map(ID))
    token = _mint(core.claim_authorities, tenant_id=uuid4(), roles=["admin"]).token
    assert await statuses(core, token) == {**OK, "/perm": 403}  # ENVIRONMENT=dev
    with process_environment(None):
        assert await statuses(core, token) == {**ALL_401, "/tenant": 400}


def test_a_token_without_the_marker_is_unaffected_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal is the marker's, not a production rule on every token."""
    plain = tokens.create_service_token({"sub": str(uuid4()), "token_type": "access"})
    monkeypatch.setattr(settings, "environment", Environment.prod)
    assert tokens.decode_token(plain) is not None


@pytest.mark.parametrize("value", [True, False, None, 0, "", "no"])
def test_the_marker_refuses_whatever_its_value(
    value: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Presence decides, not truthiness: no spelling of the claim slips through."""
    token = _marked(value)
    assert tokens.decode_token(token) is not None  # dev/test: a plain token
    with process_environment(None):
        assert tokens.decode_token(token) is None  # nor with it unset
    monkeypatch.setattr(settings, "environment", Environment.prod)
    assert tokens.decode_token(token) is None
