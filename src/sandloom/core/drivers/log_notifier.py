"""Built-in ``log`` notifier driver — the zero-dependency default.

It "delivers" a notification by emitting a structured log line. That makes the
notification capability usable out of the box (dev, tests, and prod-without-a-
mail-provider) without ever requiring external configuration, and gives a clear
audit trail. Swap in the ``smtp`` driver (or a plugin) via ``NOTIFIER_BACKEND`` /
``INFRA`` to actually deliver.

In production the driver advertises NO delivery features (unless
``SANDLOOM_ACCEPTED_RISKS`` accepts ``log_notifier_in_prod``): ``send()`` still
logs, but a module that declares
``CapabilityRequirement("notification", features={"email"})`` fails its
startup preflight instead of "succeeding" into the log with nobody notified.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from typing import Any

from sandloom.core.config.settings import settings
from sandloom.core.descriptors import HealthResult
from sandloom.core.infra.config import DEFAULT_INSTANCE, InstanceConfig
from sandloom.core.infra.contracts import Notification, NotifierProvider
from sandloom.core.observability.metrics_utils import safe_inc

logger = logging.getLogger("sandloom.core.notification")

#: A dev/catch-all driver accepts every channel (it only logs them).
NOTIFIER_LOG_FEATURES = frozenset({"email", "sms", "push", "inapp"})


def log_notifier_features() -> frozenset[str]:
    """The channels the log driver advertises: none in production.

    Logging is not delivery. Outside production it stands in for every
    channel; in production it claims one only with the accepted risk
    ``log_notifier_in_prod`` (``SANDLOOM_ACCEPTED_RISKS``; e.g. a demo stack).
    """
    if settings.is_prod and not settings.accepts_risk("log_notifier_in_prod"):
        return frozenset()
    return NOTIFIER_LOG_FEATURES


def recipient_fingerprint(recipient: str) -> str:
    """Stable, non-reversible tag for a recipient (e-mail / phone are PII).

    A sha256 prefix of the normalised address: lets operators correlate log
    lines for the same recipient without the address ever reaching the logs.
    """
    digest = hashlib.sha256(recipient.strip().lower().encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


class LogNotifierProvider(NotifierProvider):
    """Notifier that records each notification as a structured log line."""

    contract_version = "v1"

    @classmethod
    def features_for(cls, options: Mapping[str, Any]) -> frozenset[str]:
        return log_notifier_features()

    async def send(self, notification: Notification) -> None:
        logger.info(
            "notification.sent",
            extra={
                "channel": notification.channel,
                # Never the raw address (PII) — a stable hash instead.
                "recipient_hash": recipient_fingerprint(notification.recipient),
                "subject": notification.subject,
                "dedupe_key": notification.dedupe_key,
            },
        )
        safe_inc(
            "notifications_sent_total",
            {"channel": notification.channel, "result": "logged"},
        )

    async def health(self) -> HealthResult:
        return HealthResult(status="ok")


_default: LogNotifierProvider | None = None


def get_default_notifier() -> LogNotifierProvider:
    """Return the process-wide ``default`` log notifier (built once)."""
    global _default
    if _default is None:
        _default = LogNotifierProvider(name=DEFAULT_INSTANCE, driver="log", options={})
    return _default


def create_log_notifier(cfg: InstanceConfig) -> LogNotifierProvider:
    """Driver factory for ``notification`` capability / ``log`` driver."""
    if cfg.name == DEFAULT_INSTANCE:
        return get_default_notifier()
    return LogNotifierProvider(
        name=cfg.name, driver=cfg.driver, options=dict(cfg.options)
    )


__all__ = [
    "LogNotifierProvider",
    "create_log_notifier",
    "get_default_notifier",
    "log_notifier_features",
    "NOTIFIER_LOG_FEATURES",
]
