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

import json
import logging
import re
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.cli import doctor
from sandloom.core.config import accepted_risks, tiers
from sandloom.core.config.common import validate_service_binding
from sandloom.core.config.module_settings import SourceSnapshot
from sandloom.core.config.settings import _LOAD_SNAPSHOT, Settings
from sandloom.core.drivers.smtp_notifier import NotifierOptions, SmtpNotifierOptions

_STRONG = "9f3b7c1e5a2d4f6b8c0e1a3d5f7b9c2e4a6d8f0b1c3e5a7d9f2b4c6e8a0d1f3b"
_BASE = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/d",
    "JWT_SECRET": _STRONG,
}
_STRICT = {
    "ENVIRONMENT": "prod",
    "PRODUCTION_PROFILE": "multi_tenant_strict",
    "INFRA": {
        "database": {"default": {"tenant_rls": True}},
        "atomic_store": {"security": {"driver": "redis", "url": "redis://sec:6379/0"}},
    },
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
        "notification.use_tls",
        "AUTH_REVOCATION_FAIL_CLOSED",
        "RPC_COMMANDS_REQUIRE_SIGNATURE",
        "RPC_REPLAY_GUARD",
        "CONTRACT_COMPAT_CHECK",
        "INTERNAL_ENDPOINTS_OPEN_IN_DEV",
        "DOCS_ENABLED",
    }
)
SEMANTIC_BASELINE_COUNT = 11
#: Total semantic settings (baseline + ADR-argued). Only goes down.
SEMANTIC_COUNT_CEILING = 14


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
            actual: object = bool(
                infra.get(option, tiers._infra_option_unset(spec.name))
            )
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


_SMTP = {"driver": "smtp", "host": "mx", "from_addr": "no-reply@example.test"}


def _notification(**options: Any) -> dict[str, Any]:
    return {"notification": {"default": {**_SMTP, **options}}}


@pytest.mark.parametrize("environment", ["prod", "dev"])
def test_smtp_tls_off_is_an_override_in_every_profile(
    environment: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Turning TLS off on an smtp instance (recovery links over plain SMTP) is
    reported like every weaker semantic choice: the startup log, the metric
    source (``active_overrides``), ``/diagnostics/core`` and ``sandloom doctor``."""
    cfg = _settings(ENVIRONMENT=environment, INFRA=_notification(use_tls=False))
    found = tiers.active_overrides(cfg)
    assert [(o.setting, o.kind, o.profile) for o in found] == [
        ("INFRA.notification.default.use_tls", "weaker", environment)
    ]
    log = logging.getLogger("sandloom.test.config_tiers")
    with caplog.at_level(logging.INFO, logger="sandloom.test.config_tiers"):
        tiers.log_overrides(cfg, log)
    assert {r.levelno for r in caplog.records} == {
        logging.WARNING if environment == "prod" else logging.INFO
    }
    block = tiers.diagnostics(cfg)
    assert [o["setting"] for o in block["overrides"]] == [  # type: ignore[attr-defined]
        "INFRA.notification.default.use_tls"
    ]
    report = doctor.Report()
    doctor.check_overrides(report, cfg)
    assert [c.name for c in report.checks] == [
        "override:INFRA.notification.default.use_tls"
    ]


@pytest.mark.parametrize(
    "infra",
    [
        {},  # no notification instance written: the log driver, no TLS at all
        _notification(),  # TLS omitted: the smtp driver's default is on
        _notification(use_tls=True),
        {"notification": {"default": {"driver": "log"}}},  # no option to weaken
    ],
)
def test_smtp_tls_left_on_or_unset_is_no_override(infra: dict[str, Any]) -> None:
    assert tiers.active_overrides(_settings(ENVIRONMENT="prod", INFRA=infra)) == []


def test_the_unset_tls_default_is_the_smtp_drivers_own() -> None:
    """``_INFRA_OPTION_UNSET`` repeats the smtp driver's ``use_tls`` default: the
    override report would call a driver default an override (or hide a weakening)
    if the two drifted. Read from the driver's options class, not copied."""
    unset = tiers._INFRA_OPTION_UNSET["notification.use_tls"]
    assert isinstance(unset, bool)
    for options in (NotifierOptions, SmtpNotifierOptions):
        assert options.model_fields["use_tls"].default is unset


def test_smtp_tls_off_in_the_composition_selection_is_an_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The report reads the resolved selection: a composition (``DEFAULT_INFRA``
    pin) whose smtp default has TLS off is reported though ``INFRA`` is empty."""
    import sandloom.core.infra.config as infra_config

    monkeypatch.setattr(infra_config, "_APP", None)
    monkeypatch.setattr(
        infra_config,
        "_PIN",
        infra_config._checked(
            {"notification": {"default": {**_SMTP, "use_tls": False}}}
        ),
    )
    cfg = _settings(ENVIRONMENT="prod", NOTIFIER_BACKEND="smtp")
    # Labelled by where the value was written: ``INFRA`` states nothing here.
    assert [o.setting for o in tiers.active_overrides(cfg)] == [
        "notification.default.use_tls (composition DEFAULT_INFRA)"
    ]


def test_a_malformed_infra_does_not_break_the_override_report() -> None:
    """Loading the configuration refuses a malformed ``INFRA`` at startup; the
    report still answers from what the setting declares."""
    cfg = _settings(
        ENVIRONMENT="prod",
        INFRA={"notification": {"default": {"use_tls": False}}},  # no driver
    )
    assert [o.setting for o in tiers.active_overrides(cfg)] == [
        "INFRA.notification.default.use_tls"
    ]


def test_a_named_smtp_instance_with_tls_off_is_reported_by_its_name() -> None:
    cfg = _settings(
        ENVIRONMENT="prod",
        INFRA={
            "notification": {
                "default": _SMTP,
                "alerts": {**_SMTP, "use_tls": False},
            }
        },
    )
    assert [o.setting for o in tiers.active_overrides(cfg)] == [
        "INFRA.notification.alerts.use_tls"
    ]


def test_stronger_choice_is_not_an_override() -> None:
    cfg = _settings(
        ENVIRONMENT="dev",
        AUTH_REVOCATION_FAIL_CLOSED="true",
        DOCS_ENABLED="false",
        OUTBOX_RELAY_ENABLED="on",
    )
    assert tiers.active_overrides(cfg) == []


def test_the_generated_dev_default_is_logged_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``sandloom new`` writes INTERNAL_ENDPOINTS_OPEN_IN_DEV=true into the dev
    ``.env``: an override nobody chose by hand, so it is DEBUG, not a line
    every command prints (a weaker dev choice made by hand stays at INFO)."""
    cfg = _settings(ENVIRONMENT="dev", INTERNAL_ENDPOINTS_OPEN_IN_DEV="true")
    log = logging.getLogger("sandloom.test.config_tiers")
    with caplog.at_level(logging.DEBUG, logger="sandloom.test.config_tiers"):
        found = tiers.log_overrides(cfg, log)
    assert [(o.setting, o.profile) for o in found] == [
        ("INTERNAL_ENDPOINTS_OPEN_IN_DEV", "dev")
    ]
    assert {r.levelno for r in caplog.records} == {logging.DEBUG}
    # Never honoured in prod, so it is not an override there.
    prod = _settings(ENVIRONMENT="prod", INTERNAL_ENDPOINTS_OPEN_IN_DEV="true")
    assert tiers.active_overrides(prod) == []


def test_dev_opt_in_is_logged_at_info(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A weaker choice made by hand outside prod (one ``sandloom new`` did not
    generate) is reported at INFO."""
    monkeypatch.setattr(tiers, "GENERATED_DEV_DEFAULTS", {})
    chosen = _settings(ENVIRONMENT="dev", INTERNAL_ENDPOINTS_OPEN_IN_DEV="true")
    log = logging.getLogger("sandloom.test.config_tiers")
    with caplog.at_level(logging.DEBUG, logger="sandloom.test.config_tiers"):
        found = tiers.log_overrides(chosen, log)
    assert [(o.setting, o.profile) for o in found] == [
        ("INTERNAL_ENDPOINTS_OPEN_IN_DEV", "dev")
    ]
    assert {r.levelno for r in caplog.records} == {logging.INFO}


# ------------------------------------------------------- accepted risks ----


def test_every_risk_is_documented() -> None:
    for item in accepted_risks.RISKS:
        assert item.weakens and item.consulted_by, item.id
    assert "relay_disabled" in accepted_risks.RISK_IDS


@pytest.mark.parametrize(
    "raw",
    ['["weak_password_hash", "relay_disabled"]', "weak_password_hash, relay_disabled"],
)
def test_accepted_risks_parse_json_or_comma_list(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANDLOOM_ACCEPTED_RISKS", raw)
    cfg = _settings(ENVIRONMENT="prod")
    assert cfg.sandloom_accepted_risks == ["weak_password_hash", "relay_disabled"]
    assert cfg.accepts_risk("relay_disabled")
    assert not cfg.accepts_risk("local_storage_in_prod")
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
    if removed.inert is None:
        # No inert value: whatever it is set to fails startup, empty and "off"
        # spellings included (a flat url that is "turned off" still names a url
        # nothing reads any more).
        active = "redis://h:6379/0"
        for value in ("", "0", "false", "none", active):
            with pytest.raises(ValueError, match="is no longer supported"):
                _settings(**{removed.name: value})
        with caplog.at_level(logging.WARNING, logger="sandloom.settings"):
            _settings()
        assert f"{removed.name} was removed" not in caplog.text
    else:
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
    # Settings names are case-insensitive: the lower-case spelling in the
    # process environment is refused the same way, not ignored.
    monkeypatch.delenv(removed.name)
    monkeypatch.setenv(removed.name.lower(), active)
    with pytest.raises(ValueError, match=removed.name):
        _settings()
    # Both spellings absent again: nothing is refused (the check is not a
    # blanket failure).
    monkeypatch.delenv(removed.name.lower())
    _settings()


@pytest.mark.parametrize(
    "name",
    [
        "CACHE_URL",
        "BROKER_URL",
        "CACHE_SOCKET_TIMEOUT_SECONDS",
        "CACHE_SOCKET_CONNECT_TIMEOUT_SECONDS",
        "CACHE_HEALTH_CHECK_INTERVAL_SECONDS",
        "CACHE_MAX_CONNECTIONS",
        "CACHE_POOL_TIMEOUT_SECONDS",
        "CACHE_RETRY_ATTEMPTS",
    ],
)
def test_the_flat_redis_settings_are_refused_with_the_infra_replacement(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    removed = next(r for r in tiers.REMOVED if r.name == name)
    assert removed.inert is None
    assert name not in tiers.OPS
    # A setting in a ``.env`` file is refused as well as one in the environment.
    env_file = tmp_path / ".env"
    env_file.write_text(f"{name}=1\n")
    for key, value in _BASE.items():
        monkeypatch.setenv(key, str(value))
    with pytest.raises(ValueError, match=f"{name} was removed"):
        Settings(_env_file=str(env_file))
    assert "INFRA" in removed.hint or "instance" in removed.hint


@pytest.mark.parametrize("name", ["CACHE_URL", "BROKER_URL"])
@pytest.mark.parametrize("content", ["", "redis://h:6379/0"])
def test_a_removed_url_in_the_secrets_dir_is_refused_empty_or_not(
    name: str, content: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A url delivered as a file of ``SECRETS_DIR`` (the usual prod way) is as
    much a value as an environment variable: refused, empty included, without
    echoing it. Both the direct ``Settings()`` and the process load."""
    (tmp_path / name).write_text(content)
    monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SECRETS_DIR", str(tmp_path))
    with pytest.raises(ValueError, match=f"{name} was removed") as direct:
        _settings()
    assert "h:6379" not in str(direct.value)
    snapshot = SourceSnapshot(environ=dict(_BASE), env_file=None, secrets_dir=tmp_path)
    token = _LOAD_SNAPSHOT.set(snapshot)
    try:
        with pytest.raises(ValueError, match=f"{name} was removed") as loaded:
            _settings()
    finally:
        _LOAD_SNAPSHOT.reset(token)
    assert "h:6379" not in str(loaded.value)


@pytest.mark.parametrize("name", ["CACHE_URL", "BROKER_URL"])
def test_an_empty_removed_url_in_dotenv_is_refused_on_the_process_load(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``BROKER_URL=`` in ``.env`` is a value too: the doc says an empty value of
    these settings fails startup, and the load reads the snapshot's one parse."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"{name}=\n")
    monkeypatch.delenv(name, raising=False)
    snapshot = SourceSnapshot(environ=dict(_BASE), env_file=env_file)
    token = _LOAD_SNAPSHOT.set(snapshot)
    try:
        with pytest.raises(ValueError, match=f"{name} was removed"):
            _settings()
    finally:
        _LOAD_SNAPSHOT.reset(token)


def test_a_removed_setting_is_refused_first_and_another_failure_still_raises() -> None:
    with pytest.raises(ValueError) as caught:
        _settings(CACHE_RETRY_ATTEMPTS="1", SANDLOOM_ACCEPTED_RISKS="no_such_risk")
    assert "CACHE_RETRY_ATTEMPTS was removed" in str(caught.value)
    with pytest.raises(ValueError, match="unknown risk id"):
        _settings(SANDLOOM_ACCEPTED_RISKS="no_such_risk")


def test_the_renamed_semaphore_limit_is_a_setting_and_the_old_name_is_refused() -> None:
    assert _settings().EVENT_BUS_BROADCAST_SEMAPHORE_LIMIT == 100
    configured = _settings(EVENT_BUS_BROADCAST_SEMAPHORE_LIMIT="7")
    assert configured.EVENT_BUS_BROADCAST_SEMAPHORE_LIMIT == 7
    with pytest.raises(ValueError) as caught:
        _settings(EVENT_BUS_REDIS_SEMAPHORE_LIMIT="7")
    assert "EVENT_BUS_REDIS_SEMAPHORE_LIMIT was removed" in str(caught.value)
    assert "EVENT_BUS_BROADCAST_SEMAPHORE_LIMIT" in str(caught.value)


def test_the_old_semaphore_name_in_lower_case_env_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("event_bus_redis_semaphore_limit", "5")
    with pytest.raises(ValueError, match="EVENT_BUS_REDIS_SEMAPHORE_LIMIT was removed"):
        _settings()


def test_a_name_that_only_upper_cases_to_a_removed_one_is_not_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pydantic folds a name with ``lower()``: ``ſ`` (U+017F) stays itself there,
    while ``upper()`` turns it into the ``S`` of ``SEMAPHORE``. Such a name is
    no removed setting, in the environment or among the values handed over."""
    name = "event_bus_redis_ſemaphore_limit"
    assert name.upper() == "EVENT_BUS_REDIS_SEMAPHORE_LIMIT"
    monkeypatch.setenv(name, "5")
    _settings()
    _settings(**{name: "5"})


def test_an_unparsable_tracing_url_is_refused_without_echoing_its_password() -> None:
    """urllib's ``ValueError`` for a netloc that NFKC-normalizes to a delimiter
    carries the whole netloc, ``user:password`` included."""
    url = "https://collector:pw-secret：x@otel:4317"
    with pytest.raises(ValueError, match="TRACING_URL must be") as caught:
        _settings(TRACING_URL=url)
    assert "pw-secret" not in str(caught.value)


def test_an_unparsable_service_binding_url_is_refused_without_echoing_its_password() -> (
    None
):
    url = "https://u:pw-secret：x@tenancy:2222"
    with pytest.raises(ValueError, match="base_url must be an absolute") as caught:
        validate_service_binding("tenancy", {"base_url": url})
    assert "pw-secret" not in str(caught.value)
    with pytest.raises(ValueError, match="base_url must be an absolute") as caught:
        _settings(SERVICE_BINDINGS={"tenancy": {"base_url": url}})
    assert "pw-secret" not in str(caught.value)


@pytest.mark.parametrize("value", ["0", "-1"])
def test_the_semaphore_limit_below_one_is_refused_at_settings_load(value: str) -> None:
    # 0 would block every broadcast callback forever; a negative value only
    # failed when the bus was built.
    with pytest.raises(ValueError, match="EVENT_BUS_BROADCAST_SEMAPHORE_LIMIT"):
        _settings(EVENT_BUS_BROADCAST_SEMAPHORE_LIMIT=value)


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


# ------------------------------------------------- documented INFRA examples
def _documented_infra(text: str) -> list[dict[str, Any]]:
    """Every ``INFRA={...}`` JSON object a prose or message states."""
    decoder = json.JSONDecoder()
    return [
        decoder.raw_decode(text, match.end())[0]
        for match in re.finditer(r"INFRA=(?=\{)", text)
    ]


def _lease_upgrade_texts() -> list[str]:
    changelog = Path(__file__).resolve().parents[4] / "CHANGELOG.md"
    texts = [removed.hint for removed in tiers.REMOVED]
    if changelog.exists():
        texts += [
            line
            for line in changelog.read_text().splitlines()
            if "BEAT_LEASE_CACHE_INSTANCE" in line
        ]
    return [t for t in texts if "lease" in t and "INFRA=" in t]


def test_the_documented_lease_upgrade_examples_parse_into_instance_options() -> None:
    from sandloom.core.infra.config import load_infra_config

    texts = _lease_upgrade_texts()
    assert len(texts) >= 2, "the setting's message and the changelog state an example"
    everything = [e for text in texts for e in _documented_infra(text)]
    for example in everything:  # every stated example is a valid INFRA
        assert load_infra_config(_settings(INFRA=example))
    examples = [e for e in everything if "lease" in e]
    assert len(examples) >= 2
    for example in examples:
        beat = example["lease"]["beat"]
        assert "options" not in beat, "INFRA takes the options flat, next to driver"
        instance = load_infra_config(_settings(INFRA=example))["lease"]["beat"]
        assert instance.driver == beat["driver"]
        assert (
            instance.options.items()
            >= {k: v for k, v in beat.items() if k != "driver"}.items()
        )
