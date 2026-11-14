"""``NotifierConformance``: what a ``notification`` driver must do (spec 10)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from functools import partial
from typing import ClassVar, cast, get_args

import pytest

from sandloom.core.infra.ports.notification import (
    NOTIFICATION_CAPABILITY,
    Notification,
    NotificationChannel,
    Notifier,
    NotifierProvider,
)
from sandloom.driverkit.kits._harness import Call, ProviderConformance, require

_MARKER = "sandloom-conformance-marker"
_CHANNELS: tuple[NotificationChannel, ...] = get_args(NotificationChannel)


def _advertised_channels(provider: NotifierProvider) -> list[NotificationChannel]:
    channels = provider.features() & set(_CHANNELS)
    return [cast("NotificationChannel", channel) for channel in sorted(channels)]


def _message(
    channel: NotificationChannel, recipients: Mapping[str, str]
) -> Notification:
    return Notification(
        channel=channel,
        recipient=recipients[channel],
        subject="Sandloom driver conformance",
        body=f"body {_MARKER}",
        body_fields={"link": _MARKER},
    )


@pytest.mark.asyncio
class NotifierConformance(ProviderConformance[NotifierProvider]):
    """What a ``notification`` driver must do, after the harness's checks. It
    sends real messages: point ``make_provider`` at a test backend. Each
    channel is a behaviour feature: an undeclared channel's ``send`` raises
    ``Unsupported(channel)``. Logs are read through the root logger, so a
    logger with ``propagate=False`` is not checked.
    """

    contract = NotifierProvider
    protocol = Notifier
    spec = NOTIFICATION_CAPABILITY

    #: Where each channel sends. Override for your test backend.
    recipients: ClassVar[Mapping[str, str]] = {
        "email": "driver-test@example.com",
        "sms": "+15550100000",
        "push": "driver-test-device",
        "inapp": "driver-test-user",
    }

    def feature_calls(self, provider: NotifierProvider) -> Mapping[str, Call]:
        return {
            channel: partial(provider.send, _message(channel, self.recipients))
            for channel in _CHANNELS
        }

    def operation_calls(self, provider: NotifierProvider) -> Mapping[str, Call]:
        channels = _advertised_channels(provider)
        channel: NotificationChannel = channels[0] if channels else "email"
        return {"send": partial(provider.send, _message(channel, self.recipients))}

    async def test_sends_on_each_advertised_channel(
        self, provider: NotifierProvider
    ) -> None:
        channels = _advertised_channels(provider)
        if not channels:
            pytest.skip("this driver advertises no channel: nothing was sent")
        for channel in channels:
            await provider.send(_message(channel, self.recipients))

    async def test_accepts_a_dedupe_key(self, provider: NotifierProvider) -> None:
        channels = _advertised_channels(provider)
        if not channels:
            pytest.skip("this driver advertises no channel: nothing was sent")
        message = replace(
            _message(channels[0], self.recipients), dedupe_key="conformance-dedupe"
        )
        # Advisory: a driver may suppress the duplicate, neither send may fail.
        await provider.send(message)
        await provider.send(message)

    async def test_keeps_the_body_out_of_the_logs(
        self, provider: NotifierProvider, caplog: pytest.LogCaptureFixture
    ) -> None:
        channels = _advertised_channels(provider)
        if not channels:
            pytest.skip("this driver advertises no channel: nothing was sent")
        with caplog.at_level(logging.DEBUG):
            for channel in channels:
                await provider.send(_message(channel, self.recipients))
        logged = [f"{r.getMessage()} {r.__dict__}" for r in caplog.records]
        require(
            not any(_MARKER in line for line in logged),
            "a body or body_fields value was logged; these can hold secret links",
        )


__all__ = ["NotifierConformance"]
