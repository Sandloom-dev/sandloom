"""Built-in ``smtp`` notifier driver — email delivery via the stdlib.

Constructs no client at import (the connection opens per send), so it adds no
dependency and no startup I/O. It delivers the ``email`` channel only; other
channels raise so a misconfiguration is loud rather than silently dropped. The
blocking ``smtplib`` call runs in a worker thread to stay off the event loop.

TLS: ``use_tls`` upgrades with STARTTLS under ``ssl.create_default_context()``
(certificate and hostname verified — a bare ``starttls()`` verifies nothing).
In production, credentials without ``use_tls`` are refused: the login would
send the password in clear.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from collections.abc import Mapping
from email.message import EmailMessage
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sandloom.core.config.settings import settings
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.contracts import Notification, NotifierProvider
from sandloom.core.observability.metrics_utils import safe_inc

#: This driver delivers email and nothing else.
NOTIFIER_SMTP_FEATURES = frozenset({"email"})


class NotifierOptions(BaseModel):
    """Config-shape schema for the built-in notifier drivers (``log``/``smtp``).

    The default config synthesizes the SMTP connection fields for whichever
    notifier driver is selected, so both drivers share this schema (``log``
    ignores the values, ``smtp`` uses them). All optional; ``extra="forbid"``
    rejects a typo'd key (e.g. ``hots`` for ``host``) at startup.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    host: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None
    from_addr: str | None = None
    use_tls: bool | None = None


class SmtpNotifierOptions(NotifierOptions):
    """SMTP-specific build-free validation; the log notifier needs no host."""

    host: str = Field(min_length=1)

    @field_validator("host", mode="before")
    @classmethod
    def _host(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class SmtpNotifierProvider(NotifierProvider):
    """Email notifier backed by ``smtplib`` (host/port/credentials from options)."""

    def features(self) -> frozenset[str]:
        return NOTIFIER_SMTP_FEATURES

    async def send(self, notification: Notification) -> None:
        if notification.channel != "email":
            raise ValueError(
                f"smtp notifier delivers 'email' only, got {notification.channel!r}; "
                "configure a driver that advertises this channel"
            )
        message = EmailMessage()
        message["From"] = str(self.options.get("from_addr") or "no-reply@example.com")
        message["To"] = notification.recipient
        message["Subject"] = notification.subject
        message.set_content(notification.body)
        await asyncio.to_thread(self._send_sync, message)
        safe_inc("notifications_sent_total", {"channel": "email", "result": "sent"})

    def _send_sync(self, message: EmailMessage) -> None:
        _refuse_cleartext_login_in_prod(self.options)
        host = str(self.options.get("host") or "localhost").strip()
        port = int(self.options.get("port") or 25)
        with smtplib.SMTP(host, port, timeout=10) as client:
            if self.options.get("use_tls"):
                client.starttls(context=ssl.create_default_context())
            username = self.options.get("username")
            password = self.options.get("password")
            if username and password:
                client.login(str(username), str(password))
            client.send_message(message)


def _refuse_cleartext_login_in_prod(options: Mapping[str, Any]) -> None:
    """Credentials over a connection without TLS are refused in prod."""

    if (
        settings.is_prod
        and options.get("username")
        and options.get("password")
        and not options.get("use_tls")
    ):
        raise ValueError(
            "smtp notifier: SMTP credentials without use_tls would send the "
            "password in clear; set SMTP_USE_TLS=true (STARTTLS, verified) in prod"
        )


def create_smtp_notifier(cfg: InstanceConfig) -> SmtpNotifierProvider:
    """Driver factory for ``notification`` capability / ``smtp`` driver."""
    if not str(cfg.options.get("host") or "").strip():
        raise ValueError("smtp notifier requires a non-empty host (SMTP_HOST)")
    _refuse_cleartext_login_in_prod(cfg.options)
    return SmtpNotifierProvider(
        name=cfg.name, driver=cfg.driver, options=dict(cfg.options)
    )


__all__ = [
    "SmtpNotifierProvider",
    "NotifierOptions",
    "create_smtp_notifier",
    "NOTIFIER_SMTP_FEATURES",
]
