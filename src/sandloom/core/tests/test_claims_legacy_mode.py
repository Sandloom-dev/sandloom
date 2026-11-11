"""The ``legacy_tokens`` mode made visible and kept one per deployment
(ADR-0014 decision 4, accepted risk G-RISK-14).

* ``/diagnostics/core`` (``claims.legacy_tokens``) and ``sandloom doctor``
  (section ``claims``) show the mode: ``on`` with ``until``, the window end
  (``until + ACCESS_TOKEN_EXPIRE_MINUTES + JWT_LEEWAY_SECONDS``) and the time
  left; ``ended``; ``off``; diagnostics with the acceptances counted per key;
* the deploy-profile boot check (SANDLOOM-T014) refuses services that
  disagree on ``legacy_tokens`` and, once one enforces (stage 3), a service
  below the adoption release (it reports no claim policy version).
"""

from __future__ import annotations

import json
import subprocess
import time
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI

from sandloom.core.app_factory.ops import _claims_diagnostics
from sandloom.core.auth import claims_view as view
from sandloom.core.auth import tokens
from sandloom.core.claims import CPV_VERSION, ClaimAuthorities
from sandloom.core.cli import doctor
from sandloom.core.config.settings import settings
from sandloom.core.facade import CoreFacade
from sandloom.core.runtime import use_active_core
from sandloom.core.testing import deploy_profile
from sandloom.core.testing.auth import mint_test_token

LIFETIME = settings.access_token_expire_minutes * 60
LEEWAY = settings.jwt_leeway_seconds
UNTIL = 2_000_000_000.0
WINDOW_END = UNTIL + LIFETIME + LEEWAY


def _policy(until: float | None) -> ClaimAuthorities:
    return ClaimAuthorities(
        authorities={"roles": "sandloom_identity"},
        owners={"roles": "identity"},
        issuer="sandloom_identity",
        assigned=True,
        legacy_until=until,
        effective_at=time.time() - 60,
    )


def _state(until: float | None, now: float) -> dict[str, Any]:
    return view.legacy_tokens_state(
        _policy(until), now=now, lifetime_seconds=LIFETIME, leeway_seconds=LEEWAY
    )


# --- the state -----------------------------------------------------------------
def test_off_without_legacy_tokens() -> None:
    assert _state(None, UNTIL) == {
        "mode": "off",
        "cpv_version": CPV_VERSION,
        "accepted": {},
    }


def test_on_until_the_window_end_with_the_time_left() -> None:
    state = _state(UNTIL, UNTIL - 100)
    assert state["mode"] == view.LEGACY_ON
    assert state["until"] == "2033-05-18T03:33:20Z"
    assert state["window_end"] == time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(WINDOW_END)
    )
    assert state["seconds_left"] == int(WINDOW_END - (UNTIL - 100))
    assert state["risk"] == "G-RISK-14"


@pytest.mark.parametrize("now", [WINDOW_END, WINDOW_END + 86_400])
def test_ended_at_and_after_the_window_end(now: float) -> None:
    state = _state(UNTIL, now)
    assert state["mode"] == view.LEGACY_ENDED and state["seconds_left"] == 0


def test_the_acceptances_are_read_per_key() -> None:
    metrics = {
        view.legacy_metric_name("roles"): 3,
        view.legacy_metric_name("tenant_ids"): 1,
        "access_denied_total": 7,
    }
    assert view.legacy_accepted(metrics) == {"roles": 3, "tenant_ids": 1}


# --- /diagnostics/core: shown and counted ----------------------------------------
def test_diagnostics_show_the_mode_and_count_each_acceptance() -> None:
    """A token without ``cpv`` trusted through ``decode_token`` is counted
    and the diagnostics block reports it with the mode."""
    now = time.time()
    core = CoreFacade(app=FastAPI())
    core._assign_claim_authorities(_policy(now + 600))
    # A token of the release before ``cpv``: the test authority mints the
    # stated provenance and no ``cpv``.
    token = mint_test_token(
        {
            "sub": str(uuid4()),
            "token_type": "access",
            "roles": ["admin"],
            "claims_sources": ["identity"],
            "claims_sources_failed": [],
            "cpv": None,
        }
    )
    tokens._verified_tokens.clear()
    with use_active_core(core):
        claims = tokens.decode_token(token)
    tokens._verified_tokens.clear()
    assert claims is not None and claims["roles"] == ["admin"]
    block = _claims_diagnostics(core)["legacy_tokens"]
    assert block["mode"] == view.LEGACY_ON
    assert block["accepted"] == {"roles": 1}
    assert 0 < block["seconds_left"] <= 600 + LIFETIME + LEEWAY


def test_diagnostics_show_off_by_default() -> None:
    core = CoreFacade(app=FastAPI())
    assert _claims_diagnostics(core)["legacy_tokens"]["mode"] == view.LEGACY_OFF


# --- sandloom doctor ---------------------------------------------------------------
@pytest.mark.parametrize(
    ("until", "status", "text"),
    [
        (None, doctor.OK, "off"),
        (time.time() + 600, doctor.WARN, "compatibility mode ON"),
        (time.time() - LIFETIME - LEEWAY - 1, doctor.WARN, "window ended"),
    ],
    ids=["off", "on", "ended"],
)
def test_doctor_shows_the_mode(until: float | None, status: str, text: str) -> None:
    core = CoreFacade(app=FastAPI())
    core._assign_claim_authorities(_policy(until))
    report = doctor.Report()
    doctor.check_claims(report, core)
    (check,) = report.checks
    assert (check.section, check.name, check.status) == (
        "claims",
        "legacy_tokens",
        status,
    )
    assert text in check.message
    assert check.details["cpv_version"] == CPV_VERSION
    if until is not None:
        assert check.details["window_end"] in check.message


@pytest.mark.asyncio
async def test_doctor_runs_the_claims_section_and_skips_it_on_request() -> None:
    core = SimpleNamespace(claim_authorities=_policy(time.time() + 600))
    skip = frozenset(set(doctor.SECTIONS) - {"claims"})
    report = await doctor.run_doctor(skip=skip, core_loader=lambda: core)
    assert [(c.section, c.status) for c in report.checks] == [("claims", doctor.WARN)]
    every = frozenset(doctor.SECTIONS)
    report = await doctor.run_doctor(skip=every, core_loader=lambda: core)
    assert report.checks == []


# --- T014: one mode per deployment, the adoption release gate ---------------------
OFF = {"mode": "off", "cpv_version": CPV_VERSION}
ON = {"mode": "on", "cpv_version": CPV_VERSION, "until": "2026-12-01T00:00:00Z"}
ENDED = {**ON, "mode": "ended"}
LATER = {**ON, "until": "2026-12-02T00:00:00Z"}


@pytest.mark.parametrize(
    ("modes", "refused"),
    [
        ({"identity": OFF, "billing": OFF}, []),
        ({"identity": ON, "billing": ON}, []),
        ({"identity": ON, "billing": ENDED}, []),  # one argument, one clock
        ({"identity": ON, "billing": OFF}, ["billing", "identity"]),
        ({"identity": ON, "billing": LATER}, ["billing", "identity"]),
        # stage 1-2: a service still on the old release is allowed while
        # every new one runs the compatibility mode ...
        ({"identity": None, "billing": ON}, []),
        # ... and refused once one enforces (stage 3)
        ({"identity": None, "billing": OFF}, ["identity"]),
    ],
    ids=[
        "all-off",
        "all-on",
        "on-and-ended",
        "mixed",
        "two-untils",
        "old-release-in-stage-2",
        "old-release-in-stage-3",
    ],
)
def test_the_profile_keeps_one_mode(modes: dict[str, Any], refused: list[str]) -> None:
    problems = deploy_profile.claims_mode_problems(modes)
    assert sorted(problems) == refused
    for texts in problems.values():
        assert all(deploy_profile.CODE in text for text in texts)


def test_the_boot_check_reads_the_mode_from_the_doctor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``boot_problems`` compares what each booted service's doctor reports
    (``claims/legacy_tokens`` details)."""
    report = {
        "checks": [
            {
                "section": "claims",
                "name": "legacy_tokens",
                "status": "warn",
                "message": "compatibility mode ON",
                "details": ON,
            }
        ]
    }

    def run(*_args: Any, **_kwargs: Any) -> Any:
        return subprocess.CompletedProcess([], 0, stdout=json.dumps(report), stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    failures, mode = deploy_profile._boot({})
    assert failures == [] and mode == ON
