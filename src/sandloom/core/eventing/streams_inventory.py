"""Streams bus diagnostics: subscription inventory, group gauges, audience checks."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import (
    Any,
)

from prometheus_client import Counter, Gauge

from sandloom.core.eventing.bus_base import BaseEventBus
from sandloom.core.eventing.transport_capabilities import TransportCapabilities
from sandloom.core.eventing.transport_capabilities import TransportCapability as Cap

logger = logging.getLogger("sandloom.core.eventing.event_bus")


_VERSION_SUFFIX_RE = re.compile(r"^(?P<base>.+)\.v[0-9]+$")

_METRIC_NO_CONSUMERS = Counter(
    "sandloom_events_published_without_consumers_total",
    "Publishes to a channel with no live consumer group in the inventory",
    ["channel"],
)
_METRIC_VERSION_STARVATION = Counter(
    "sandloom_events_version_starvation_total",
    "Publishes to one channel version while another version's consumers starve",
    ["channel", "starved"],
)
_GAUGE_GROUP_LAG = Gauge(
    "sandloom_streams_group_lag",
    "Entries in the stream not yet delivered to the consumer group",
    ["stream", "group"],
    multiprocess_mode="livemax",
)
_GAUGE_GROUP_PENDING = Gauge(
    "sandloom_streams_group_pending",
    "Entries delivered to the consumer group but not yet acknowledged",
    ["stream", "group"],
    multiprocess_mode="livemax",
)
_GAUGE_GROUP_OLDEST_AGE = Gauge(
    "sandloom_streams_group_oldest_unacked_age_seconds",
    "Age of the oldest entry the consumer group has not acknowledged (0 = none)",
    ["stream", "group"],
    multiprocess_mode="livemostrecent",
)


def _channel_base(channel: str) -> str | None:
    """``billing.paid.v2`` -> ``billing.paid`` (``None`` if unversioned)."""
    match = _VERSION_SUFFIX_RE.match(channel)
    return match.group("base") if match else None


class _StreamsInventory(BaseEventBus):
    """Inventory heartbeat, lag / pending / age gauges, publish audience check."""

    _transport: Any
    _capabilities: TransportCapabilities
    _inventory_info: dict[tuple[str, str], dict[str, Any]]
    _group_stats: dict[tuple[str, str], dict[str, Any]]
    _last_published: dict[str, float]
    _audience_checked: dict[str, float]

    # Per-channel throttle for the publish-side "anyone listening?" check.
    _AUDIENCE_CHECK_SECONDS = 60.0
    _AUDIENCE_DELAY_SECONDS = 1.0

    def _schedule_audience_check(self, channel: str) -> None:
        """Run :meth:`_check_audience` off the publish path (throttled).

        Delayed slightly so the sibling versions of a dual-publish (written
        back-to-back) are all recorded before the starvation check looks.
        """
        if Cap.LIST_CONSUMERS not in self._capabilities:
            return
        now = time.monotonic()
        window = self._AUDIENCE_CHECK_SECONDS
        if now - self._audience_checked.get(channel, float("-inf")) < window:
            return
        self._audience_checked[channel] = now

        async def _later() -> None:
            await asyncio.sleep(self._AUDIENCE_DELAY_SECONDS)
            await self._check_audience(channel)

        self._track_task(asyncio.create_task(_later()))

    async def _check_audience(self, channel: str) -> None:
        """Warn (throttled) when a publish has no live consumer to read it.

        Two silent-starvation shapes are surfaced from the shared subscription
        inventory: (a) nobody is subscribed to ``channel`` at all, and (b) the
        channel's OTHER versions have live consumers this process no longer
        publishes to -- the v1 -> v2 rollout where a producer switched to
        ``.v2`` while another service still consumes ``.v1`` (dual-publish with
        ``publish_event_versions`` until every consumer has migrated).
        Best-effort and never raises: diagnostics must not fail a publish.
        """
        if Cap.LIST_CONSUMERS not in self._capabilities:
            return
        now = time.monotonic()
        window = self._AUDIENCE_CHECK_SECONDS
        try:
            consumers = await self._transport.list_consumers()
        except Exception:  # noqa: BLE001 - diagnostics only
            return
        if not consumers:
            return  # no inventory at all (not populated by any consumer yet)
        streams: dict[str, dict[str, Any]] = {}
        for entry in consumers:
            streams.setdefault(str(entry.get("stream")), entry)
        if channel not in streams:
            _METRIC_NO_CONSUMERS.labels(channel=channel).inc()
            logger.warning(
                "published to %s but no live consumer group is registered for it "
                "(events are retained only for the stream retention window)",
                channel,
            )
        base = _channel_base(channel)
        if base is None:
            return
        for stream, entry in sorted(streams.items()):
            if stream == channel or _channel_base(stream) != base:
                continue
            last = self._last_published.get(stream)
            if last is not None and now - last < window * 2:
                continue  # dual-publishing: the sibling version is still fed
            _METRIC_VERSION_STARVATION.labels(channel=channel, starved=stream).inc()
            logger.warning(
                "published to %s while consumer group %s (module=%s service=%s) "
                "still reads %s, which this process no longer publishes: those "
                "consumers starve. Dual-publish both versions "
                "(publish_event_versions) until every consumer migrated.",
                channel,
                entry.get("group"),
                entry.get("module"),
                entry.get("service"),
                stream,
            )

    def group_stats(self, channel: str, group: str) -> dict[str, Any] | None:
        """Last observed lag / pending of a local subscription's group."""
        stats = self._group_stats.get((channel, group))
        return dict(stats) if stats is not None else None

    async def _observe_oldest_age(
        self, channel: str, group: str, item: dict[str, Any]
    ) -> None:
        """Refresh the group's oldest-unacked age (one extra command / beat)."""
        if Cap.OLDEST_UNACKED_AGE not in self._capabilities:
            return
        try:
            age = await self._transport.oldest_unacked_age_seconds(
                channel,
                group,
                last_delivered_id=item.get("last_delivered_id"),
                pending=item.get("pending"),
                lag=item.get("lag"),
            )
        except Exception:  # diagnostics only
            logger.debug("oldest-unacked age probe failed", exc_info=True)
            return
        if age is not None:
            _GAUGE_GROUP_OLDEST_AGE.labels(stream=channel, group=group).set(age)
            self._group_stats[(channel, group)]["oldest_unacked_age_seconds"] = age

    async def _heartbeat(self, channel: str, group: str) -> None:
        """Refresh the group lag/pending gauges and the inventory entry.

        The inventory entry carries the group's ``last_delivered_id``: a
        process that lost its in-memory position recreates a group lost to
        NOGROUP there instead of at ``"$"`` (see the Streams transport).
        """
        info = self._inventory_info.get((channel, group))
        groups: list[dict[str, Any]] = []
        if Cap.GROUP_INFO in self._capabilities:
            try:
                groups = await self._transport.group_info(channel)
            except Exception:  # noqa: BLE001 - diagnostics only
                groups = []
        for item in groups:
            if item.get("group") != group:
                continue
            lag, pending = item.get("lag"), item.get("pending")
            self._group_stats[(channel, group)] = {
                "lag": lag,
                "pending": pending,
                "observed_at": time.time(),
            }
            if lag is not None:
                _GAUGE_GROUP_LAG.labels(stream=channel, group=group).set(lag)
            if pending is not None:
                _GAUGE_GROUP_PENDING.labels(stream=channel, group=group).set(pending)
            await self._observe_oldest_age(channel, group, item)
            if info is not None and item.get("last_delivered_id"):
                info["last_delivered_id"] = str(item["last_delivered_id"])
        if info is not None and Cap.REGISTER_CONSUMER in self._capabilities:
            try:
                await self._transport.register_consumer(channel, group, info)
            except Exception:  # diagnostics only
                logger.debug("inventory heartbeat failed", exc_info=True)
