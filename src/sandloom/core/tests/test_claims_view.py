"""The trusted view of a verified access token (ADR-0014 decisions 3, 4).

Unit level: :func:`sandloom.core.auth.claims_view.apply_view` over payloads,
no app. The one condition (allowed, ``cpv`` digest, kept source), the
``previous=`` rollout, the identity -> acme_identity key change matrix
(steps 0-3, rollback a-c, negative rows), the ``legacy_tokens`` window and
its end, the 401 / 503 split, and the one platform admission.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import HTTPException

from sandloom.core.app_factory.claim_authorities import (
    build_claim_authorities,
    filter_to_composition,
)
from sandloom.core.auth import claims_view as view
from sandloom.core.claims import ClaimAuthorities, cpv_digest
from sandloom.core.tests._entry_points_support import installed_entry_points

LIFETIME = 900.0
LEEWAY = 10.0
NOW = 2_000_000_000.0
ID = "sandloom_identity"
ACME = "acme.modules.acme_identity"


@pytest.fixture(autouse=True)
def _standard_entry_points() -> Iterator[None]:
    """``sandloom_identity`` / ``sandloom_tenancy`` are the modules
    ``identity`` / ``tenancy`` by their entry points, installed or not."""
    with installed_entry_points():
        yield


def policy(
    roles: tuple[str, str] = (ID, "identity"),
    previous: tuple[str, str] | None = None,
    *,
    legacy_until: float | None = None,
    effective_at: float = NOW - 3600,
) -> ClaimAuthorities:
    path, owner = roles
    return ClaimAuthorities(
        authorities={"roles": path, "permissions": path},
        owners={"roles": owner, "permissions": owner},
        issuer=path,
        assigned=True,
        previous={"roles": previous[0]} if previous else {},
        previous_owners={"roles": previous[1]} if previous else {},
        legacy_until=legacy_until,
        effective_at=effective_at,
    )


def token(
    *,
    sources: list[str],
    digest_of: str | None = ID,
    failed: list[str] | None = None,
    iat: float = NOW - 60,
    exp: float | None = None,
    **claims: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sub": "u",
        "token_type": "access",
        "roles": ["admin"],
        "iat": iat,
        "exp": exp if exp is not None else iat + LIFETIME,
        "claims_sources": sources,
        "claims_sources_failed": failed or [],
        **claims,
    }
    if digest_of is not None:
        payload["cpv"] = {"v": 1, "k": {"roles": cpv_digest("roles", digest_of)}}
    return payload


def apply(
    payload: dict[str, Any], pol: ClaimAuthorities, now: float = NOW
) -> dict[str, Any]:
    view.apply_view(
        payload, pol, now=now, lifetime_seconds=LIFETIME, leeway_seconds=LEEWAY
    )
    return payload


def roles_reason(payload: dict[str, Any]) -> str | None:
    return view.untrusted_reasons(payload).get("roles")


def test_the_one_condition_trusts_the_current_authority() -> None:
    out = apply(token(sources=["identity"]), policy())
    assert out["roles"] == ["admin"] and view.UNTRUSTED_KEY not in out


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (token(sources=[]), view.SOURCE_ABSENT),
        (token(sources=["identity"], failed=["identity"]), view.SOURCE_ABSENT),
        (token(sources=["identity"], digest_of="app.modules.identity"), view.MISMATCH),
        (token(sources=["identity"], digest_of=None), view.OUTDATED),
    ],
    ids=["source-absent", "source-failed", "app-local-identity", "no-cpv"],
)
def test_a_part_missing_drops_the_key(payload: dict[str, Any], reason: str) -> None:
    out = apply(payload, policy())
    assert "roles" not in out and roles_reason(out) == reason


def test_a_forged_untrusted_marker_is_never_read_from_the_token() -> None:
    payload = token(sources=["identity"])
    payload[view.UNTRUSTED_KEY] = {"roles": "anything"}
    assert view.UNTRUSTED_KEY not in apply(payload, policy())


def test_an_unassigned_key_is_a_definite_deny() -> None:
    """A key a module declares (a fail-safe declaration or an imported claim
    atom's key, B-N12) that the map does not assign: nobody vouches."""
    pol = dataclasses.replace(policy(), declared=frozenset({"features"}))
    out = apply(token(sources=["identity"], features=["x"]), pol)
    assert "features" not in out
    assert view.untrusted_reasons(out)["features"] == view.UNASSIGNED


def test_a_key_nothing_declares_is_not_the_kernels_to_protect() -> None:
    """B-N12: the kernel names no module's key; undeclared and unassigned,
    ``features`` is an ordinary claim (no claim atom here reads it)."""
    out = apply(token(sources=["identity"], features=["x"]), policy())
    assert out["features"] == ["x"]


def test_another_digest_issued_before_the_map_took_effect_is_outdated() -> None:
    pol = policy(effective_at=NOW)
    out = apply(token(sources=["identity"], digest_of=ACME, iat=NOW - 5), pol)
    assert roles_reason(out) == view.OUTDATED


def test_a_refresh_or_other_token_type_is_left_alone() -> None:
    payload = token(sources=[], token_type="refresh")
    assert apply(payload, policy())["roles"] == ["admin"]


def test_the_tenant_selectors_fall_with_the_grant() -> None:
    pol = ClaimAuthorities(
        authorities={"tenant_ids": "sandloom_tenancy"},
        owners={"tenant_ids": "tenancy"},
        assigned=True,
        effective_at=NOW - 3600,
    )
    payload = token(sources=[], digest_of=None, tenant_ids=["t"], tenant_id="t")
    payload["cpv"] = {
        "v": 1,
        "k": {"tenant_ids": cpv_digest("tenant_ids", "sandloom_tenancy")},
    }
    out = apply(payload, pol)
    assert "tenant_ids" not in out and "tenant_id" not in out
    assert view.untrusted_reasons(out)["tenant_id"] == view.SOURCE_ABSENT


# --- the key change identity -> acme_identity (ADR-0014 decision 4) ------
M1 = (ID, "identity")
M2 = (ACME, "acme_identity")
T1: dict[str, Any] = {"sources": ["identity"], "digest_of": ID}
T2: dict[str, Any] = {"sources": ["acme_identity"], "digest_of": ACME}


@pytest.mark.parametrize(
    ("step", "verifier", "t1", "t2"),
    [
        ("0", policy(M1), None, view.MISMATCH),
        ("1", policy(M2, previous=M1), None, None),
        ("2", policy(M2, previous=M1), None, None),
        ("3", policy(M2, effective_at=NOW - 30), view.OUTDATED, None),
        ("rollback a", policy(M1, previous=M2), None, None),
        ("rollback b", policy(M1, previous=M2), None, None),
        ("rollback c", policy(M1, effective_at=NOW - 30), None, view.OUTDATED),
    ],
)
def test_the_key_change_matrix(
    step: str, verifier: ClaimAuthorities, t1: str | None, t2: str | None
) -> None:
    # T1 and T2 were minted before the step's deploy (iat NOW - 60).
    assert roles_reason(apply(token(**T1), verifier)) == t1, step
    assert roles_reason(apply(token(**T2), verifier)) == t2, step


@pytest.mark.parametrize(
    "payload",
    [
        token(sources=["acme_identity"], digest_of=ID),
        token(sources=["identity"], digest_of=ACME),
        token(sources=["identity"], digest_of="app.modules.identity"),
        token(sources=["identity"], failed=["identity"], digest_of=ID),
    ],
    ids=["identity-digest-acme-source", "reverse", "app-local", "failed"],
)
def test_the_negative_rows_at_step_2(payload: dict[str, Any]) -> None:
    assert "roles" not in apply(payload, policy(M2, previous=M1))


def test_changing_one_key_leaves_the_others_trusted() -> None:
    pol = ClaimAuthorities(
        authorities={"roles": ID, "permissions": ACME},
        owners={"roles": "identity", "permissions": "acme_identity"},
        assigned=True,
        effective_at=NOW - 3600,
    )
    payload = token(sources=["identity"], permissions=["p"])
    payload["cpv"]["k"]["permissions"] = cpv_digest("permissions", ID)
    out = apply(payload, pol)
    assert out["roles"] == ["admin"] and "permissions" not in out


# --- the initial enablement: legacy_tokens (G-RISK-14) -------------------
UNTIL = NOW + 600


@pytest.mark.parametrize(
    ("payload", "now", "trusted"),
    [
        (token(sources=["identity"], digest_of=None), NOW, True),
        (token(sources=[], digest_of=None), NOW, False),
        (token(sources=["identity"], digest_of=None, iat=UNTIL), UNTIL + 1, False),
        (
            token(sources=["identity"], digest_of=None, exp=NOW + LIFETIME + 1),
            NOW,
            False,
        ),
        (
            token(sources=["identity"], digest_of=None, iat=UNTIL - 1),
            UNTIL + LIFETIME + LEEWAY,
            False,
        ),
    ],
    ids=[
        "inside",
        "authority-absent",
        "iat-at-until",
        "lifetime-exceeded",
        "window-end",
    ],
)
def test_the_compatibility_window(
    payload: dict[str, Any], now: float, trusted: bool
) -> None:
    out = apply(payload, policy(legacy_until=UNTIL), now=now)
    assert ("roles" in out) is trusted


@pytest.mark.parametrize(
    "cpv",
    [{"v": 999, "k": {}}, {}, None, "invalid"],
    ids=["unknown-version", "missing-version", "null", "invalid-type"],
)
def test_present_cpv_cannot_use_the_compatibility_window(cpv: Any) -> None:
    payload = token(sources=["identity"], digest_of=None, cpv=cpv)
    valid_until, legacy_keys = view.apply_view(
        payload,
        policy(legacy_until=UNTIL),
        now=NOW,
        lifetime_seconds=LIFETIME,
        leeway_seconds=LEEWAY,
    )
    assert "roles" not in payload
    assert view.untrusted_reasons(payload)["roles"] == view.OUTDATED
    assert valid_until == float("inf")
    assert not legacy_keys


def test_the_compatibility_view_expires_at_the_window_end() -> None:
    until, keys = view.apply_view(
        token(sources=["identity"], digest_of=None),
        policy(legacy_until=UNTIL),
        now=NOW,
        lifetime_seconds=LIFETIME,
        leeway_seconds=LEEWAY,
    )
    assert until == UNTIL + LIFETIME + LEEWAY and keys == ("roles",)


def test_without_legacy_tokens_a_token_without_cpv_is_outdated_even_for_absent_keys() -> (
    None
):
    payload = token(sources=["identity"], digest_of=None)
    del payload["roles"]
    out = apply(payload, policy())
    assert view.untrusted_reasons(out) == {
        "roles": view.OUTDATED,
        "permissions": view.OUTDATED,
    }


# --- the one platform admission ------------------------------------------
@pytest.mark.parametrize(
    ("untrusted", "status"),
    [
        ({}, 403),
        ({"roles": view.OUTDATED}, 401),
        ({"roles": view.MISMATCH}, 503),
        ({"roles": view.SOURCE_ABSENT}, 403),
    ],
)
def test_platform_admission_status(untrusted: dict[str, str], status: int) -> None:
    with pytest.raises(HTTPException) as exc:
        view.platform_admission([], untrusted)
    assert exc.value.status_code == status


def test_platform_admission_admits_a_trusted_admin_whatever_the_issuer_is_named() -> (
    None
):
    out = apply(token(**T2), policy(M2))
    view.platform_admission(out["roles"], view.untrusted_reasons(out))


# --- the window end (G-RISK-14), the argument still written --------------
WINDOW_END = UNTIL + LIFETIME + LEEWAY
FORGED = {"roles": ["admin"], "tenant_ids": ["t-foreign"], "tenant_id": "t-foreign"}
WINDOW_MAP = {
    "issuer": ID,
    "roles": ID,
    "permissions": ID,
    "tenant_ids": "sandloom_tenancy",
}


def window_policy(placement: str) -> ClaimAuthorities:
    """The verifier with ``legacy_tokens`` still written: the monolith (both
    authorities loaded) or a split service running neither."""
    loaded = (
        {"identity": ID, "tenancy": "sandloom_tenancy"} if placement == "mono" else {}
    )
    bindings = {} if placement == "mono" else {"tenancy.claims.v1": {"service_id": "t"}}
    policy = filter_to_composition(
        build_claim_authorities(WINDOW_MAP), loaded=loaded, bindings=bindings
    )
    return dataclasses.replace(policy, legacy_until=UNTIL, effective_at=NOW - 3600)


def forged_token(iat: float) -> dict[str, Any]:
    return token(sources=["identity", "tenancy"], digest_of=None, iat=iat, **FORGED)


@pytest.mark.parametrize("placement", ["mono", "split"])
def test_inside_the_window_a_token_without_cpv_is_trusted(placement: str) -> None:
    """The control: the same token inside the window keeps its claims."""
    out = apply(forged_token(UNTIL - 1), window_policy(placement), now=UNTIL)
    assert out["roles"] == ["admin"] and out["tenant_ids"] == ["t-foreign"]


@pytest.mark.parametrize("placement", ["mono", "split"])
@pytest.mark.parametrize(
    ("iat", "now"),
    [
        (UNTIL - 1, WINDOW_END),
        (UNTIL - 1, WINDOW_END + 86_400),
        (UNTIL, UNTIL + 1),
        (UNTIL + 5, UNTIL + 10),
    ],
    ids=["at-window-end", "after-window-end", "iat-at-until", "iat-after-until"],
)
def test_a_token_without_cpv_gives_no_rights_outside_the_window(
    placement: str, iat: float, now: float
) -> None:
    """Forged ``roles`` / ``tenant_ids`` / ``tenant_id`` in a token without
    ``cpv`` give no rights at or after the window end, nor with ``iat`` at or
    after ``until``, while ``legacy_tokens`` is still written (ADR-0014
    decision 4, G-RISK-14)."""
    out = apply(forged_token(iat), window_policy(placement), now=now)
    for key in FORGED:
        assert key not in out, key
    reasons = view.untrusted_reasons(out)
    assert reasons["roles"] == reasons["tenant_ids"] == view.OUTDATED
    assert reasons["tenant_id"] == view.OUTDATED
    with pytest.raises(HTTPException) as refused:
        view.platform_admission(out.get("roles"), reasons)
    assert refused.value.status_code == 401
