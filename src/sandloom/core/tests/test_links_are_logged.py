"""``links_are_logged``: ``NOTIFIER_LOG_LINKS`` acts only through the ``log`` driver.

A module that prints a token for a dev without a frontend asks this, not the
flag alone: with ``smtp`` (or a plugin driver) a real mail goes out and the
setting does nothing, so a token on stderr would reach the log pipeline next
to a delivered mail.
"""

from __future__ import annotations

import pytest

from sandloom.core.config.settings import settings
from sandloom.core.drivers.log_notifier import (
    NOTIFIER_LOG_FEATURES,
    create_log_notifier,
)
from sandloom.core.drivers.smtp_notifier import (
    NOTIFIER_SMTP_FEATURES,
    create_smtp_notifier,
)
from sandloom.core.infra.access import links_are_logged
from sandloom.core.infra.active import use_infra
from sandloom.core.infra.capabilities import NOTIFICATION_CAPABILITY
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.registry import InfraRegistry


def _registry() -> InfraRegistry:
    registry = InfraRegistry(strict_capabilities=True)
    registry.register_capability(NOTIFICATION_CAPABILITY)
    registry.register_driver(
        "notification", "log", create_log_notifier, features=NOTIFIER_LOG_FEATURES
    )
    registry.register_driver(
        "notification", "smtp", create_smtp_notifier, features=NOTIFIER_SMTP_FEATURES
    )
    registry.set_config(
        {
            "notification": {
                "default": InstanceConfig("notification", "default", "log", {}),
                "mail": InstanceConfig(
                    "notification", "mail", "smtp", {"host": "mail.invalid"}
                ),
            }
        }
    )
    return registry


def test_the_flag_with_the_log_driver_logs_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "notifier_log_links", True)
    with use_infra(_registry()):
        assert links_are_logged() is True


def test_the_log_driver_without_the_flag_logs_no_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "notifier_log_links", False)
    with use_infra(_registry()):
        assert links_are_logged() is False


def test_the_flag_with_the_smtp_driver_logs_no_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mail is delivered and the setting does nothing: the answer is no."""
    monkeypatch.setattr(settings, "notifier_log_links", True)
    with use_infra(_registry()):
        assert links_are_logged("mail") is False


def test_the_answer_is_for_the_named_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "notifier_log_links", True)
    with use_infra(_registry()):
        assert links_are_logged("default") is True
        assert links_are_logged("mail") is False
