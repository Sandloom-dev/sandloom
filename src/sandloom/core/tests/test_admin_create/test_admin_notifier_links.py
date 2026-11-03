"""``NOTIFIER_LOG_LINKS``: the log notifier shows an email's links in dev only."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from sandloom.core.config import tiers
from sandloom.core.config.settings import Settings, settings
from sandloom.core.drivers.log_notifier import LogNotifierProvider
from sandloom.core.infra.contracts import Notification

_BASE = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/d",
    "CACHE_URL": "redis://cache-redis:6379/0",
    "BROKER_URL": "redis://cache-redis:6379/1",
    "JWT_SECRET": "9f3b7c1e5a2d4f6b8c0e1a3d5f7b9c2e4a6d8f0b1c3e5a7d9f2b4c6e8a0d1f3b",
}
LINK = "http://127.0.0.1:8000/auth/verify-email?token=abc.def"


def _settings(**env: Any) -> Settings:
    return Settings(_env_file=None, **{**_BASE, **env})  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("NOTIFIER_LOG_LINKS", "SANDLOOM_ACCEPTED_RISKS", "ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)


def test_it_is_an_ops_setting_and_off_by_default() -> None:
    assert "NOTIFIER_LOG_LINKS" in tiers.OPS
    assert _settings(ENVIRONMENT="dev").notifier_log_links is False


def test_production_refuses_it_without_the_accepted_risk() -> None:
    with pytest.raises(ValueError, match="NOTIFIER_LOG_LINKS"):
        _settings(ENVIRONMENT="prod", NOTIFIER_LOG_LINKS=True)
    accepted = _settings(
        ENVIRONMENT="prod",
        NOTIFIER_LOG_LINKS=True,
        SANDLOOM_ACCEPTED_RISKS='["log_links_in_prod"]',
    )
    assert accepted.notifier_log_links is True
    assert _settings(ENVIRONMENT="dev", NOTIFIER_LOG_LINKS=True).notifier_log_links


def _mail() -> Notification:
    return Notification(
        channel="email",
        recipient="owner@example.com",
        subject="Verify your email",
        body=f"Hello,\n\nOpen {LINK} to verify.\n",
    )


async def test_the_links_are_shown_marked_dev_only(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider = LogNotifierProvider(name="default", driver="log", options={})
    monkeypatch.setattr(settings, "notifier_log_links", False)
    with caplog.at_level(logging.INFO, logger="sandloom.core.notification"):
        await provider.send(_mail())
    assert LINK not in capsys.readouterr().err
    monkeypatch.setattr(settings, "notifier_log_links", True)
    with caplog.at_level(logging.INFO, logger="sandloom.core.notification"):
        await provider.send(_mail())
    shown = capsys.readouterr().err
    assert f"[dev only] Verify your email: {LINK}" in shown
    assert "owner@example.com" not in shown + caplog.text
