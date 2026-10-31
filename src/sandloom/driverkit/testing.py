"""Conformance kits: subclass one in your driver's tests, provide ``make_provider``.

Each obligation is its own test, so a failure names what to fix. Requires pytest
and pytest-asyncio.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Mapping
from typing import ClassVar, cast, get_args

import pytest
import pytest_asyncio

from sandloom.core.infra.capabilities import NOTIFICATION_CAPABILITY
from sandloom.core.infra.contracts import NotificationChannel
from sandloom.core.infra.registry import versions_compatible
from sandloom.driverkit import HealthResult, Notification, NotifierProvider

_MARKER = "sandloom-conformance-marker"


def _require(condition: bool, what: str) -> None:
    if not condition:
        raise AssertionError(what)


def _advertised_channels(provider: NotifierProvider) -> list[NotificationChannel]:
    channels = provider.features() & set(get_args(NotificationChannel))
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
class NotifierConformance:
    """What a ``notification`` driver must do. It sends real messages: point
    ``make_provider`` at a test backend. Logs are read through the root logger, so
    a logger with ``propagate=False`` is not checked.
    """

    #: Where each channel sends. Override for your test backend.
    recipients: ClassVar[Mapping[str, str]] = {
        "email": "driver-test@example.com",
        "sms": "+15550100000",
        "push": "driver-test-device",
        "inapp": "driver-test-user",
    }

    @pytest.fixture
    def make_provider(self) -> Callable[[], NotifierProvider]:
        raise NotImplementedError("override make_provider to build your driver")

    @pytest_asyncio.fixture
    async def provider(
        self, make_provider: Callable[[], NotifierProvider]
    ) -> AsyncIterator[NotifierProvider]:
        built = make_provider()
        await built.aopen()
        yield built
        await built.aclose()

    async def test_implements_the_contract(self, provider: NotifierProvider) -> None:
        _require(
            isinstance(provider, NotifierProvider),
            f"{type(provider).__name__} must subclass NotifierProvider",
        )

    async def test_states_the_contract_version(
        self, provider: NotifierProvider
    ) -> None:
        version = getattr(type(provider), "contract_version", None)
        _require(
            isinstance(version, str)
            and versions_compatible(NOTIFICATION_CAPABILITY.version, version),
            f"contract_version must match {NOTIFICATION_CAPABILITY.version!r}, got "
            f"{version!r}",
        )

    async def test_advertises_only_defined_features(
        self, provider: NotifierProvider
    ) -> None:
        unknown = provider.features() - NOTIFICATION_CAPABILITY.features
        _require(not unknown, f"advertises undefined feature(s): {sorted(unknown)}")

    async def test_reports_the_features_it_declares(
        self, provider: NotifierProvider
    ) -> None:
        declared = type(provider).features_for(provider.options)
        _require(
            provider.features() == declared,
            "declare features in supported_features or features_for; do not "
            "override features()",
        )

    async def test_sends_on_each_advertised_channel(
        self, provider: NotifierProvider
    ) -> None:
        channels = _advertised_channels(provider)
        if not channels:
            pytest.skip("this driver advertises no channel: nothing was sent")
        for channel in channels:
            await provider.send(_message(channel, self.recipients))

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
        _require(
            not any(_MARKER in line for line in logged),
            "a body or body_fields value was logged; these can hold secret links",
        )

    async def test_reports_health(self, provider: NotifierProvider) -> None:
        _require(
            isinstance(await provider.health(), HealthResult),
            "health() must return a HealthResult",
        )

    async def test_closes_more_than_once(self, provider: NotifierProvider) -> None:
        await provider.aclose()
        await provider.aclose()


__all__ = ["NotifierConformance"]
