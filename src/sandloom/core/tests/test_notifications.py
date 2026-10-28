"""Tests for the notification capability (log + smtp drivers)."""

from __future__ import annotations

import pytest

from sandloom.core.drivers.log_notifier import (
    NOTIFIER_LOG_FEATURES,
    LogNotifierProvider,
    create_log_notifier,
)
from sandloom.core.drivers.smtp_notifier import (
    NOTIFIER_SMTP_FEATURES,
    create_smtp_notifier,
)
from sandloom.core.infra.capabilities import NOTIFICATION_CAPABILITY
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.contracts import Notification, NotifierProvider
from sandloom.core.infra.registry import InfraRegistry


def _registry(driver: str = "log") -> InfraRegistry:
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
                "default": InstanceConfig("notification", "default", driver, {})
            }
        }
    )
    return registry


def test_default_resolves_to_log_notifier() -> None:
    provider = _registry().resolve("notification", "default")
    assert isinstance(provider, LogNotifierProvider)
    assert isinstance(provider, NotifierProvider)


def test_email_feature_is_satisfiable_by_default() -> None:
    # The log driver advertises every channel, so requiring "email" resolves.
    provider = _registry().resolve("notification", "default", features={"email"})
    assert provider is not None


@pytest.mark.asyncio
async def test_log_notifier_send_succeeds() -> None:
    provider = _registry().resolve("notification", "default")
    await provider.send(
        Notification(channel="email", recipient="a@b.c", subject="Hi", body="Body")
    )


@pytest.mark.asyncio
async def test_log_notifier_never_logs_the_raw_recipient(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from sandloom.core.drivers.log_notifier import recipient_fingerprint

    provider = _registry().resolve("notification", "default")
    with caplog.at_level("INFO", logger="sandloom.core.notification"):
        await provider.send(
            Notification(
                channel="email", recipient="Alice@Example.com", subject="Hi", body="B"
            )
        )
    record = next(r for r in caplog.records if r.getMessage() == "notification.sent")
    assert not hasattr(record, "recipient")
    assert "alice@example.com" not in str(record.__dict__).lower()
    assert record.recipient_hash == recipient_fingerprint("alice@example.com")
    assert record.recipient_hash.startswith("sha256:")


@pytest.mark.asyncio
async def test_smtp_rejects_non_email_channel() -> None:
    provider = create_smtp_notifier(
        InstanceConfig("notification", "default", "smtp", {"host": "localhost"})
    )
    assert provider.features() == {"email"}
    with pytest.raises(ValueError, match="email"):
        await provider.send(
            Notification(channel="sms", recipient="+100", subject="s", body="b")
        )


class _FakeSMTP:
    """Records what the driver asks of ``smtplib.SMTP`` (no network)."""

    instances: list[_FakeSMTP] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.starttls_kwargs: dict[str, object] | None = None
        self.logged_in = False
        _FakeSMTP.instances.append(self)

    def __enter__(self) -> _FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def starttls(self, **kwargs: object) -> None:
        self.starttls_kwargs = kwargs

    def login(self, username: str, password: str) -> None:
        self.logged_in = True

    def send_message(self, message: object) -> None:
        return None


def _email() -> Notification:
    return Notification(
        channel="email", recipient="a@example.com", subject="s", body="b"
    )


@pytest.mark.asyncio
async def test_smtp_starttls_verifies_the_server_certificate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ssl

    from sandloom.core.drivers import smtp_notifier

    _FakeSMTP.instances.clear()
    monkeypatch.setattr(smtp_notifier.smtplib, "SMTP", _FakeSMTP)
    provider = create_smtp_notifier(
        InstanceConfig(
            "notification",
            "default",
            "smtp",
            {"host": "mail", "use_tls": True, "username": "u", "password": "p"},
        )
    )
    await provider.send(_email())
    (client,) = _FakeSMTP.instances
    assert client.starttls_kwargs is not None
    context = client.starttls_kwargs.get("context")
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert client.logged_in


def test_smtp_login_without_tls_is_refused_in_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.config.settings import settings

    monkeypatch.setattr(type(settings), "is_prod", property(lambda self: True))
    creds = {"host": "mail", "username": "u", "password": "p"}
    with pytest.raises(ValueError, match="use_tls"):
        create_smtp_notifier(InstanceConfig("notification", "default", "smtp", creds))
    # With TLS (or without credentials) the driver builds.
    create_smtp_notifier(
        InstanceConfig("notification", "default", "smtp", {**creds, "use_tls": True})
    )
    create_smtp_notifier(
        InstanceConfig("notification", "default", "smtp", {"host": "mail"})
    )


# --------------------------------------------------------------------------- #
# GLM review #8: in production the log driver must not pretend to deliver.
# --------------------------------------------------------------------------- #


def _prod(monkeypatch: pytest.MonkeyPatch, *, allow: bool) -> None:
    from sandloom.core.config.settings import settings

    monkeypatch.setattr(type(settings), "is_prod", property(lambda self: True))
    monkeypatch.setattr(
        settings, "sandloom_accepted_risks", ["log_notifier_in_prod"] if allow else []
    )


def test_log_driver_advertises_no_delivery_features_in_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.drivers.bootstrap import register_builtin_drivers
    from sandloom.core.drivers.log_notifier import log_notifier_features
    from sandloom.core.infra.registry import FeatureUnsupportedError

    _prod(monkeypatch, allow=False)
    assert log_notifier_features() == frozenset()
    registry = InfraRegistry(strict_capabilities=True)
    register_builtin_drivers(registry, schema_registry=None)  # type: ignore[arg-type]
    registry.set_config(
        {
            "notification": {
                "default": InstanceConfig("notification", "default", "log", {})
            }
        }
    )
    # The capability itself still resolves (send() logs), but a module that
    # REQUIRES email delivery fails instead of "succeeding" into the log.
    assert registry.resolve("notification", "default") is not None
    with pytest.raises(FeatureUnsupportedError):
        registry.resolve("notification", "default", features={"email"})
    provider = create_log_notifier(InstanceConfig("notification", "x", "log", {}))
    assert provider.features() == frozenset()


def test_log_driver_delivery_features_in_prod_are_an_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.drivers.log_notifier import log_notifier_features

    _prod(monkeypatch, allow=True)
    assert log_notifier_features() == NOTIFIER_LOG_FEATURES
