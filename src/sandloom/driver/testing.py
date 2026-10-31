"""Conformance kits: what a driver must do to honour a capability's contract."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast, get_args

from sandloom.core.infra.capabilities import NOTIFICATION_CAPABILITY
from sandloom.core.infra.contracts import NotificationChannel
from sandloom.driver import HealthResult, Notification, NotifierProvider

_MARKER = "sandloom-conformance-secret"


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.text: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.text.append(f"{record.getMessage()} {record.__dict__}")


@contextmanager
def _captured_logs() -> Iterator[_Records]:
    root, records = logging.getLogger(), _Records()
    level = root.level
    root.addHandler(records)
    root.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        root.setLevel(level)
        root.removeHandler(records)


def _require(condition: bool, what: str) -> None:
    if not condition:
        raise AssertionError(f"notifier conformance: {what}")


async def assert_notifier_conformance(
    provider: NotifierProvider, *, recipient: str = "driver-test@example.com"
) -> None:
    """Raise ``AssertionError`` on the first way ``provider`` breaks the contract.

    It sends a real notification on each channel the driver advertises, so point
    the driver at a test backend. Logs are read through the root logger: a logger
    with ``propagate=False`` is not checked.
    """
    _require(
        isinstance(provider, NotifierProvider),
        f"{type(provider).__name__} does not subclass NotifierProvider",
    )
    features = provider.features()
    unknown = features - NOTIFICATION_CAPABILITY.features
    _require(not unknown, f"advertises unknown feature(s) {sorted(unknown)}")
    channels = features & set(get_args(NotificationChannel))
    with _captured_logs() as logs:
        for channel in sorted(channels):
            await provider.send(
                Notification(
                    channel=cast("NotificationChannel", channel),
                    recipient=recipient,
                    subject="Sandloom driver conformance",
                    body=f"body {_MARKER}",
                    body_fields={"link": _MARKER},
                )
            )
    _require(
        not any(_MARKER in line for line in logs.text),
        "a body or body_fields value was logged",
    )
    health = await provider.health()
    _require(isinstance(health, HealthResult), "health() must return a HealthResult")
    try:
        await provider.aclose()
        await provider.aclose()
    except Exception as exc:
        raise AssertionError(
            f"notifier conformance: aclose() must be idempotent ({exc!r})"
        ) from exc


__all__ = ["assert_notifier_conformance"]
