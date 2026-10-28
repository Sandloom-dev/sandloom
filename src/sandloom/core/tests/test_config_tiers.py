"""Configuration tiers (``sandloom.core.config.tiers``): governed, not exported.

* every ``Settings`` field sits in exactly one tier (semantic / ops) — a new
  field must be classified;
* the semantic tier is RATCHETED: its size may only go down, and a semantic
  setting outside the baseline needs an ``adr=`` reference;
* the profile table is exactly today's behaviour: each profile's default
  settings produce the table's values and no override;
* removed settings are refused at startup (their inert value only warns).
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from sandloom.core.cli import doctor
from sandloom.core.config import accepted_risks, tiers
from sandloom.core.config.settings import Settings

_STRONG = "9f3b7c1e5a2d4f6b8c0e1a3d5f7b9c2e4a6d8f0b1c3e5a7d9f2b4c6e8a0d1f3b"
_BASE = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/d",
    "CACHE_URL": "redis://cache-redis:6379/0",
    "BROKER_URL": "redis://cache-redis:6379/1",
    "JWT_SECRET": _STRONG,
}
_STRICT = {
    "ENVIRONMENT": "prod",
    "PRODUCTION_PROFILE": "multi_tenant_strict",
    "INFRA": {
        "database": {"default": {"tenant_rls": True}},
        "cache": {"security": {"driver": "redis", "url": "redis://sec:6379/0"}},
    },
    "SECURITY_CACHE_INSTANCE": "security",
}
_PROFILE_ENV: dict[str, dict[str, Any]] = {
    "dev": {"ENVIRONMENT": "dev"},
    "test": {"ENVIRONMENT": "test"},
    "prod": {"ENVIRONMENT": "prod"},
    "prod_strict": _STRICT,
}

#: The semantic tier's settings that need no ADR (introduced 2026-10-24 with
#: 30, shrunk by ADR-0006 and by dropping the RPC auth / response-binding
#: modes to these). RATCHET: remove a name when its setting
#: leaves the tier (and lower the count — it only goes down); a semantic
#: setting NOT listed here must carry ``adr=`` (the ADR that argues why the
#: framework cannot choose that trade-off itself).
SEMANTIC_BASELINE = frozenset(
    {
        "ENVIRONMENT",
        "PRODUCTION_PROFILE",
        "TENANCY_MODE",
        "database.tenant_rls",
        "SECURITY_CACHE_INSTANCE",
        "AUTH_REVOCATION_FAIL_CLOSED",
        "RPC_COMMANDS_REQUIRE_SIGNATURE",
        "RPC_REPLAY_GUARD",
        "CONTRACT_COMPAT_CHECK",
        "INTERNAL_ENDPOINTS_OPEN_IN_DEV",
        "DOCS_ENABLED",
        "BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS",
        "SMTP_USE_TLS",
    }
)
SEMANTIC_BASELINE_COUNT = 13
#: Total semantic settings (baseline + ADR-argued). Only goes down.
SEMANTIC_COUNT_CEILING = 15


@pytest.fixture(autouse=True)
def _no_ambient_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Earlier tests in the worker may leave setting env vars behind."""
    for alias in _aliases():
        monkeypatch.delenv(alias, raising=False)


def _settings(**env: Any) -> Settings:
    return Settings(_env_file=None, **{**_BASE, **env})  # type: ignore[call-arg]


def _aliases() -> set[str]:
    return {f.alias or n.upper() for n, f in Settings.model_fields.items()}


# ------------------------------------------------------------ inventory ----


def test_every_setting_is_in_exactly_one_tier() -> None:
    semantic = {s.name for s in tiers.SEMANTIC if not s.infra_option}
    ops = set(tiers.OPS)
    assert not semantic & ops, semantic & ops
    classified = semantic | ops
    unclassified = sorted(_aliases() - classified)
    assert not unclassified, (
        f"classify the new setting(s) {unclassified} in sandloom.core.config.tiers "
        "(semantic: changes a guarantee; ops: capacity / operations)"
    )
    assert not classified - _aliases(), sorted(classified - _aliases())
    names = [s.name for s in tiers.SEMANTIC]
    assert len(names) == len(set(names))
    for name in names:
        assert tiers.tier_of(name) == "semantic"


def test_semantic_tier_is_ratcheted() -> None:
    names = {s.name for s in tiers.SEMANTIC}
    stale = sorted(SEMANTIC_BASELINE - names)
    assert not stale, (
        f"{stale} left the semantic tier: drop them from SEMANTIC_BASELINE and "
        "lower SEMANTIC_BASELINE_COUNT (the count only goes down)"
    )
    assert len(SEMANTIC_BASELINE) == SEMANTIC_BASELINE_COUNT
    assert len(tiers.SEMANTIC) <= SEMANTIC_COUNT_CEILING, (
        "the semantic tier only shrinks: fold a new weakening into "
        "SANDLOOM_ACCEPTED_RISKS or let the profile choose the value"
    )
    unargued = sorted(
        s.name for s in tiers.SEMANTIC if s.name not in SEMANTIC_BASELINE and not s.adr
    )
    assert not unargued, (
        f"new semantic setting(s) {unargued}: the framework should choose the "
        "trade-off per profile instead — or reference the ADR that argues the "
        "knob (SemanticSetting(adr=...))"
    )


def test_every_semantic_setting_defines_every_profile() -> None:
    for spec in tiers.SEMANTIC:
        assert set(spec.profile) == set(tiers.PROFILES), spec.name
        assert spec.why, spec.name
        assert spec.refused <= set(tiers.PROFILES), spec.name
        for prof in spec.refused:
            assert spec.profile[prof] is not tiers.NO_OPINION, spec.name
        for value in spec.profile.values():
            if spec.strength and value is not tiers.NO_OPINION:
                assert value in spec.strength, (spec.name, value)


# --------------------------------------------- profile table == behaviour ----


@pytest.mark.parametrize("prof", tiers.PROFILES)
def test_profile_table_is_the_current_default_behaviour(prof: str) -> None:
    cfg = _settings(**_PROFILE_ENV[prof])
    assert tiers.profile_of(cfg) == prof
    for spec in tiers.SEMANTIC:
        expected = spec.profile[prof]
        if expected is tiers.NO_OPINION:
            continue
        if spec.infra_option:
            cap, option = spec.name.split(".", 1)
            infra = cfg.infra.get(cap, {}).get("default", {})
            actual: object = bool(infra.get(option, False))
        else:
            actual = spec.value(cfg)
        assert actual == expected, (spec.name, prof, actual, expected)
    assert tiers.active_overrides(cfg) == []


def test_a_keyed_split_rpc_caller_reports_no_override() -> None:
    from sandloom.core.contract_testing import rpc_env

    cfg = _settings(
        ENVIRONMENT="dev",
        SERVICE_BINDINGS={
            "tenancy.membership.v1": {"base_url": "http://t", "service_id": "tenancy"}
        },
        **rpc_env(
            "billing",
            peers=["tenancy"],
            allowed_calls={"billing": ["tenancy.membership.v1"]},
        ),
    )
    assert tiers.active_overrides(cfg) == []


# ----------------------------------------------------------- overrides ----


def test_prod_override_is_reported_loud_and_explicit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = _settings(
        ENVIRONMENT="prod",
        AUTH_REVOCATION_FAIL_CLOSED="false",
        DOCS_ENABLED="true",
    )
    found = tiers.active_overrides(cfg)
    assert {(o.setting, o.kind, o.explicit) for o in found} == {
        ("AUTH_REVOCATION_FAIL_CLOSED", "weaker", True),
        ("DOCS_ENABLED", "weaker", True),
    }
    log = logging.getLogger("sandloom.test.config_tiers")
    with caplog.at_level(logging.INFO, logger="sandloom.test.config_tiers"):
        tiers.log_overrides(cfg, log)
    assert {r.levelno for r in caplog.records} == {logging.WARNING}
    assert "CONFIG OVERRIDE: AUTH_REVOCATION_FAIL_CLOSED=false weakens the prod" in (
        caplog.text
    )
    block = tiers.diagnostics(cfg)
    assert block["profile"] == "prod"
    assert block["tiers"] == tiers.tier_counts()
    assert {o["setting"] for o in block["overrides"]} == {  # type: ignore[attr-defined]
        "AUTH_REVOCATION_FAIL_CLOSED",
        "DOCS_ENABLED",
    }
    assert block["accepted_risks"] == []


def test_stronger_choice_is_not_an_override() -> None:
    cfg = _settings(
        ENVIRONMENT="dev",
        AUTH_REVOCATION_FAIL_CLOSED="true",
        DOCS_ENABLED="false",
        OUTBOX_RELAY_ENABLED="on",
    )
    assert tiers.active_overrides(cfg) == []


def test_dev_opt_in_is_logged_at_info(caplog: pytest.LogCaptureFixture) -> None:
    cfg = _settings(ENVIRONMENT="dev", INTERNAL_ENDPOINTS_OPEN_IN_DEV="true")
    log = logging.getLogger("sandloom.test.config_tiers")
    with caplog.at_level(logging.INFO, logger="sandloom.test.config_tiers"):
        found = tiers.log_overrides(cfg, log)
    assert [(o.setting, o.profile) for o in found] == [
        ("INTERNAL_ENDPOINTS_OPEN_IN_DEV", "dev")
    ]
    assert {r.levelno for r in caplog.records} == {logging.INFO}
    # Never honoured in prod, so it is not an override there.
    prod = _settings(ENVIRONMENT="prod", INTERNAL_ENDPOINTS_OPEN_IN_DEV="true")
    assert tiers.active_overrides(prod) == []


# ------------------------------------------------------- accepted risks ----


def test_every_risk_is_documented() -> None:
    for item in accepted_risks.RISKS:
        assert item.weakens and item.consulted_by, item.id
    assert "relay_disabled" in accepted_risks.RISK_IDS


@pytest.mark.parametrize(
    "raw",
    ['["fake_psp_in_prod", "relay_disabled"]', "fake_psp_in_prod, relay_disabled"],
)
def test_accepted_risks_parse_json_or_comma_list(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANDLOOM_ACCEPTED_RISKS", raw)
    cfg = _settings(ENVIRONMENT="prod")
    assert cfg.sandloom_accepted_risks == ["fake_psp_in_prod", "relay_disabled"]
    assert cfg.accepts_risk("relay_disabled")
    assert not cfg.accepts_risk("weak_password_hash")
    with pytest.raises(KeyError):
        cfg.accepts_risk("no_such_risk")


def test_unknown_risk_id_fails_startup() -> None:
    with pytest.raises(ValueError, match="unknown risk id"):
        _settings(SANDLOOM_ACCEPTED_RISKS=["fake_psp"])


def test_accepted_risk_is_logged_loud_counted_and_listed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Outside prod a prod-only risk is inert: listed, never an override.
    dev = _settings(ENVIRONMENT="dev", SANDLOOM_ACCEPTED_RISKS=["weak_password_hash"])
    assert tiers.active_overrides(dev) == []
    assert [r["in_effect"] for r in tiers.diagnostics(dev)["accepted_risks"]] == [  # type: ignore[union-attr]
        False
    ]
    cfg = _settings(ENVIRONMENT="prod", SANDLOOM_ACCEPTED_RISKS=["weak_password_hash"])
    log = logging.getLogger("sandloom.test.config_tiers")
    with caplog.at_level(logging.INFO, logger="sandloom.test.config_tiers"):
        found = tiers.log_overrides(cfg, log)
    assert [(o.setting, o.kind) for o in found] == [
        ("SANDLOOM_ACCEPTED_RISKS[weak_password_hash]", "accepted_risk")
    ]
    assert {r.levelno for r in caplog.records} == {logging.WARNING}
    assert "ACCEPTED RISK: weak_password_hash —" in caplog.text
    block = tiers.diagnostics(cfg)
    assert block["accepted_risks"] == [
        {
            "risk": "weak_password_hash",
            "weakens": accepted_risks.risk("weak_password_hash").weakens,
            "consulted_by": accepted_risks.risk("weak_password_hash").consulted_by,
            "in_effect": True,
        }
    ]
    report = doctor.Report()
    doctor.check_overrides(report, cfg)
    assert [(c.name, c.status) for c in report.checks] == [
        ("override:SANDLOOM_ACCEPTED_RISKS[weak_password_hash]", doctor.WARN)
    ]


def test_relay_off_in_prod_is_an_override_unless_the_risk_is_accepted() -> None:
    off = _settings(ENVIRONMENT="prod", OUTBOX_RELAY_ENABLED="off")
    assert {(o.setting, o.kind) for o in tiers.active_overrides(off)} == {
        ("OUTBOX_RELAY_ENABLED", "weaker")
    }
    accepted = _settings(
        ENVIRONMENT="prod",
        OUTBOX_RELAY_ENABLED="off",
        SANDLOOM_ACCEPTED_RISKS=["relay_disabled"],
    )
    assert {(o.setting, o.kind) for o in tiers.active_overrides(accepted)} == {
        ("SANDLOOM_ACCEPTED_RISKS[relay_disabled]", "accepted_risk")
    }
    # The gunicorn config delegates the relay to one leader worker.
    delegated = _settings(ENVIRONMENT="prod", OUTBOX_RELAY_ENABLED="delegated")
    assert tiers.active_overrides(delegated) == []


# ------------------------------------------------------ removed settings ----


@pytest.mark.parametrize("removed", tiers.REMOVED, ids=lambda r: r.name)
def test_removed_setting_fails_unless_inert(
    removed: tiers.RemovedSetting,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert removed.name not in _aliases()
    inert = str(removed.inert).lower()
    active = "false" if removed.inert is True else "true"
    with caplog.at_level(logging.WARNING, logger="sandloom.settings"):
        _settings(**{removed.name: inert})
    assert f"{removed.name} was removed in {removed.removed_in}" in caplog.text
    with pytest.raises(ValueError, match="is no longer supported"):
        _settings(**{removed.name: active})
    monkeypatch.setenv(removed.name, active)
    with pytest.raises(ValueError, match=removed.name):
        _settings()


def test_doctor_lists_overrides() -> None:
    report = doctor.Report()
    doctor.check_overrides(report, _settings(ENVIRONMENT="prod", DOCS_ENABLED="true"))
    assert [(c.name, c.status) for c in report.checks] == [
        ("override:DOCS_ENABLED", doctor.WARN)
    ]
    clean = doctor.Report()
    doctor.check_overrides(clean, _settings(ENVIRONMENT="prod"))
    assert [(c.name, c.status) for c in clean.checks] == [("overrides", doctor.OK)]


# -------------------------------------------------------------- removal ----


# ----------------------------------------------------------------- docs ----


def test_generated_reference_lists_every_tier() -> None:
    page = tiers.render_markdown()
    counts = tiers.tier_counts()
    assert f"**{counts['semantic']} semantic**" in page
    for spec in tiers.SEMANTIC:
        assert spec.why in page, spec.name
    for name in tiers.OPS:
        assert f"`{name}`" in page
    assert (
        "| `AUTH_REVOCATION_FAIL_CLOSED` | `false` | `false` | `true` | `true` (refused) |"
        in page
    )
