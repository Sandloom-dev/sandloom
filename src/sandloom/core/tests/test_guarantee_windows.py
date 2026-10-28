"""Guarantee windows (ADR-0005): the settings bounding a guarantee agree.

Deterministic checks of :mod:`sandloom.core.guarantee_windows`: the command dedupe
TTL against the relay's redelivery horizon and the inbox-purge window against
the stream retention; production refuses (unless the risk is accepted), other
environments warn. (``OUTBOX_RELAY_ENABLED=off`` is the accepted-risks list's
``relay_disabled``, not a window.)
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from sandloom.core import effects
from sandloom.core import guarantee_windows as gw


def _settings(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "is_prod": False,
        "contract_command_dedupe_ttl_seconds": 86400,
        "outbox_relay_enabled": "auto",
        "outbox_relay_max_attempts": 10,
        "outbox_relay_backoff_base_seconds": 2.0,
        "outbox_relay_backoff_max_seconds": 300.0,
        "event_stream_retention_seconds": 7 * 86400,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _ids(problems: list[gw.Problem]) -> list[str]:
    return [p.risk_id for p in problems]


def test_defaults_hold_every_window() -> None:
    assert gw.guarantee_window_problems(_settings(), inbox_days=8) == []
    assert gw.guarantee_window_problems(_settings(is_prod=True)) == []
    # the relay switch is not a window (accepted-risks list: relay_disabled)
    assert gw.guarantee_window_problems(_settings(outbox_relay_enabled="off")) == []


def test_relay_horizon_is_the_capped_backoff_sum() -> None:
    # 2 4 8 16 32 64 128 256 300 300
    assert effects.relay_retry_horizon_seconds(_settings()) == 1110.0
    assert (
        effects.relay_retry_horizon_seconds(_settings(outbox_relay_max_attempts=1))
        == 2.0
    )


def test_command_ttl_below_the_relay_horizon() -> None:
    problems = gw.guarantee_window_problems(
        _settings(contract_command_dedupe_ttl_seconds=600)
    )
    assert _ids(problems) == [gw.COMMAND_DEDUPE_BELOW_RETRY_HORIZON]
    assert "CONTRACT_COMMAND_DEDUPE_TTL_SECONDS=600" in problems[0].message


@pytest.mark.parametrize(
    ("retention", "days", "risky"),
    [
        (7 * 86400, 8, False),
        (7 * 86400, 7.9, True),  # inside retention + 1 day
        (7 * 86400, 1, True),
        (3600, 1.05, False),
        (0, 365, True),  # unbounded retention: any purge
    ],
)
def test_inbox_purge_window(retention: int, days: float, risky: bool) -> None:
    problems = gw.inbox_purge_problems(
        _settings(event_stream_retention_seconds=retention), days
    )
    assert _ids(problems) == ([gw.INBOX_PURGE_BELOW_REDELIVERY] if risky else [])


def test_production_refuses_and_names_the_risk() -> None:
    bad = _settings(is_prod=True, contract_command_dedupe_ttl_seconds=60)
    with pytest.raises(
        gw.GuaranteeWindowError, match=r"\[command_dedupe_below_retry_horizon\]"
    ):
        gw.enforce_guarantee_windows(bad)


def test_dev_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="sandloom.core.guarantee_windows"):
        problems = gw.enforce_guarantee_windows(
            _settings(contract_command_dedupe_ttl_seconds=10)
        )
    assert _ids(problems) == [gw.COMMAND_DEDUPE_BELOW_RETRY_HORIZON]
    assert "command_dedupe_below_retry_horizon" in caplog.text


def test_accepted_risks_are_not_refused() -> None:
    prod = _settings(
        is_prod=True,
        contract_command_dedupe_ttl_seconds=60,
        sandloom_accepted_risks=[gw.COMMAND_DEDUPE_BELOW_RETRY_HORIZON],
    )
    assert gw.enforce_guarantee_windows(prod) == []
    listed = [gw.COMMAND_DEDUPE_BELOW_RETRY_HORIZON, "relay_disabled"]
    assert gw.accepted_risks(_settings(sandloom_accepted_risks=listed)) == set(listed)
    assert gw.accepted_risks(_settings()) == frozenset()
    # an explicit list overrides the settings' own
    with pytest.raises(gw.GuaranteeWindowError):
        gw.enforce_guarantee_windows(prod, accepted=())


def test_window_risk_ids_are_registered_accepted_risks() -> None:
    from sandloom.core.config import accepted_risks

    assert gw.COMMAND_DEDUPE_BELOW_RETRY_HORIZON in accepted_risks.RISK_IDS


def test_create_app_refuses_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    from sandloom.core import app_factory
    from sandloom.core.config.settings import settings

    monkeypatch.setattr(settings, "contract_command_dedupe_ttl_seconds", 5)
    monkeypatch.setattr(settings, "environment", "prod")
    with pytest.raises(gw.GuaranteeWindowError, match="command_dedupe"):
        app_factory.create_app()
