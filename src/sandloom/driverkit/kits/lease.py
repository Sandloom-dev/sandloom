"""``LeaseConformance``: what a ``lease`` driver must do (spec 5).

A driver's tests subclass the kit and override ``make_provider`` (an unopened
provider; the harness checks that it opens, closes twice, declares its
features, reports health and its isolation domain before these groups). The
groups below need real time for the TTL (a 3 second lease is the shortest the
contract allows), so a whole run takes about ten seconds. Every test uses names
of its own: a lease outlives a test on a shared backend.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Mapping
from dataclasses import replace
from functools import partial
from itertools import pairwise

import pytest

from sandloom.core.infra.errors import InvalidArgument
from sandloom.core.infra.ports.lease import (
    LEASE_CAPABILITY,
    MAX_LEASE_NAME_LENGTH,
    MIN_LEASE_TTL_SECONDS,
    Lease,
    LeaseProvider,
    LeaseStore,
)
from sandloom.driverkit.kits._harness import Call, ProviderConformance, require

#: The shortest TTL, in seconds, and how long to wait for it to pass.
_TTL = MIN_LEASE_TTL_SECONDS
_AFTER_EXPIRY_SECONDS = _TTL + 1.0
#: A token no grant carries.
_PLACEHOLDER = "kit-placeholder"
#: How many acquirers race for one name.
_RACERS = 8


def _granted(lease: Lease | None) -> Lease:
    """``lease``, which a free (or released, or expired) name must have granted."""
    if lease is None:
        raise AssertionError("acquire of a free name must grant the lease")
    return lease


def _name() -> str:
    return f"kit:lease:{uuid.uuid4().hex[:12]}"


@pytest.mark.asyncio
class LeaseConformance(ProviderConformance[LeaseProvider]):
    """What a ``lease`` driver must do, after the harness's checks."""

    contract = LeaseProvider
    protocol = LeaseStore
    spec = LEASE_CAPABILITY

    def operation_calls(self, provider: LeaseProvider) -> Mapping[str, Call]:
        name = _name()
        lease = Lease(
            name=name,
            owner="kit",
            token=_PLACEHOLDER,
            fence=1,
            ttl_seconds=_TTL,
        )
        return {
            "acquire": partial(provider.acquire, name, ttl_seconds=_TTL, owner="kit"),
            "renew": partial(provider.renew, lease),
            "release": partial(provider.release, lease),
            "holder": partial(provider.holder, name),
        }

    # ------------------------------------------------------------ acquire
    async def test_acquire_grants_a_free_name(self, provider: LeaseProvider) -> None:
        name = _name()
        lease = _granted(await provider.acquire(name, ttl_seconds=_TTL, owner="kit:1"))
        require(
            (lease.name, lease.owner, lease.ttl_seconds) == (name, "kit:1", _TTL),
            "the Lease must carry the name, owner and ttl_seconds it was acquired with",
        )
        require(
            isinstance(lease.token, str) and bool(lease.token),
            "the Lease token must be a non-empty string",
        )
        require(
            isinstance(lease.fence, int) and lease.fence >= 1,
            "the Lease fence must be an int >= 1",
        )

    async def test_one_of_many_concurrent_acquirers_wins(
        self, provider: LeaseProvider
    ) -> None:
        name = _name()
        results = await asyncio.gather(
            *(
                provider.acquire(name, ttl_seconds=30, owner=f"kit:racer-{i}")
                for i in range(_RACERS)
            )
        )
        winners = [lease for lease in results if lease is not None]
        require(
            len(winners) == 1,
            f"exactly one of {_RACERS} concurrent acquirers must win, "
            f"{len(winners)} did: the lease is not exclusive",
        )
        require(len({lease.token for lease in winners}) == 1, "one grant, one token")

    async def test_a_second_acquire_of_a_held_name_answers_none(
        self, provider: LeaseProvider
    ) -> None:
        name = _name()
        first = await provider.acquire(name, ttl_seconds=30, owner="kit:a")
        require(first is not None, "the first acquire must grant the lease")
        second = await provider.acquire(name, ttl_seconds=30, owner="kit:b")
        require(second is None, "a held name must not be granted a second time")

    async def test_names_are_independent(self, provider: LeaseProvider) -> None:
        first = await provider.acquire(_name(), ttl_seconds=30, owner="kit")
        second = await provider.acquire(_name(), ttl_seconds=30, owner="kit")
        require(
            first is not None and second is not None,
            "a lease on one name must not block another name",
        )

    async def test_a_second_provider_contends_for_the_same_name(
        self, provider: LeaseProvider, make_provider: Callable[[], LeaseProvider]
    ) -> None:
        self.requires_feature(provider, "shared")
        other = make_provider()
        await other.aopen()
        try:
            name = _name()
            first = await provider.acquire(name, ttl_seconds=30, owner="kit:a")
            require(first is not None, "the first acquire must grant the lease")
            require(
                await other.acquire(name, ttl_seconds=30, owner="kit:b") is None,
                "a driver that declares 'shared' must exclude another provider "
                "of the same backend",
            )
            holder = await other.holder(name)
            require(
                holder is not None and holder.owner == "kit:a",
                "'shared': another provider must read the holder",
            )
        finally:
            await other.aclose()

    # ----------------------------------------------------------- the fence
    async def test_the_fence_grows_across_release(
        self, provider: LeaseProvider
    ) -> None:
        name = _name()
        fences: list[int] = []
        for _ in range(4):
            lease = _granted(await provider.acquire(name, ttl_seconds=30, owner="kit"))
            fences.append(lease.fence)
            require(await provider.release(lease), "the holder's release must succeed")
        require(
            all(a < b for a, b in pairwise(fences)),
            f"the fence must grow with every grant, also after release: {fences}",
        )

    async def test_release_hands_over_at_once_with_a_greater_fence(
        self, provider: LeaseProvider
    ) -> None:
        name = _name()
        first = _granted(await provider.acquire(name, ttl_seconds=30, owner="kit:a"))
        require(await provider.release(first), "the holder's release must answer True")
        second = _granted(await provider.acquire(name, ttl_seconds=30, owner="kit:b"))
        require(
            second.fence > first.fence,
            f"the fence after release ({second.fence}) must exceed the one "
            f"before ({first.fence})",
        )
        require(second.token != first.token, "every grant has its own token")

    async def test_expiry_hands_over_with_a_greater_fence(
        self, provider: LeaseProvider
    ) -> None:
        name = _name()
        first = _granted(await provider.acquire(name, ttl_seconds=_TTL, owner="kit:a"))
        require(
            await provider.acquire(name, ttl_seconds=_TTL, owner="kit:b") is None,
            "a lease within its TTL must not be granted",
        )
        await asyncio.sleep(_AFTER_EXPIRY_SECONDS)
        require(
            await provider.holder(name) is None,
            "an expired lease must read as free (holder answers None)",
        )
        require(
            not await provider.renew(first),
            "an expired lease must not be renewed, even when nobody took it",
        )
        second = _granted(await provider.acquire(name, ttl_seconds=30, owner="kit:b"))
        require(
            second.fence > first.fence,
            f"the fence after expiry ({second.fence}) must exceed the one "
            f"before ({first.fence})",
        )
        require(
            not await provider.renew(first),
            "the old holder's renew must answer False once another holds the lease",
        )

    # --------------------------------------------------------------- renew
    async def test_renew_extends_past_the_original_ttl(
        self, provider: LeaseProvider
    ) -> None:
        name = _name()
        lease = _granted(await provider.acquire(name, ttl_seconds=_TTL, owner="kit"))
        await asyncio.sleep(_TTL - 1.0)
        require(await provider.renew(lease), "the holder's renew must answer True")
        await asyncio.sleep(_TTL - 1.0)  # past the first TTL, inside the renewed one
        holder = await provider.holder(name)
        require(
            holder is not None and holder.owner == "kit",
            "a renewed lease must outlive its original TTL",
        )
        require(
            await provider.acquire(name, ttl_seconds=_TTL, owner="kit:b") is None,
            "a renewed lease must stay exclusive",
        )

    async def test_renew_keeps_the_fence(self, provider: LeaseProvider) -> None:
        name = _name()
        lease = _granted(await provider.acquire(name, ttl_seconds=30, owner="kit"))
        require(await provider.renew(lease), "the holder's renew must answer True")
        holder = await provider.holder(name)
        require(
            holder is not None and holder.fence == lease.fence,
            "renew must not change the fence",
        )

    async def test_renew_by_a_stale_token_answers_false(
        self, provider: LeaseProvider
    ) -> None:
        name = _name()
        lease = _granted(await provider.acquire(name, ttl_seconds=30, owner="kit:a"))
        forged = replace(lease, token=lease.token + "-stale")
        require(
            not await provider.renew(forged),
            "renew with a token that does not hold the lease must answer False",
        )
        require(
            await provider.renew(lease),
            "a refused renew must not disturb the real holder",
        )

    async def test_renew_by_a_displaced_holder_answers_false(
        self, provider: LeaseProvider
    ) -> None:
        name = _name()
        first = _granted(await provider.acquire(name, ttl_seconds=30, owner="kit:a"))
        await provider.release(first)
        _granted(await provider.acquire(name, ttl_seconds=30, owner="kit:b"))
        require(
            not await provider.renew(first),
            "the old holder's renew must answer False after another acquired",
        )
        holder = await provider.holder(name)
        require(
            holder is not None and holder.owner == "kit:b",
            "a refused renew must leave the new holder in place",
        )

    # ------------------------------------------------------------- release
    async def test_release_by_a_stale_token_is_a_noop(
        self, provider: LeaseProvider
    ) -> None:
        name = _name()
        first = _granted(await provider.acquire(name, ttl_seconds=30, owner="kit:a"))
        await provider.release(first)
        _granted(await provider.acquire(name, ttl_seconds=30, owner="kit:b"))
        require(
            not await provider.release(first),
            "release by a stale token must answer False",
        )
        holder = await provider.holder(name)
        require(
            holder is not None and holder.owner == "kit:b",
            "release by a stale token must not end the current holder's lease",
        )
        require(
            await provider.acquire(name, ttl_seconds=30, owner="kit:c") is None,
            "the lease must still be held after a stale release",
        )

    async def test_release_twice_answers_false_the_second_time(
        self, provider: LeaseProvider
    ) -> None:
        lease = _granted(await provider.acquire(_name(), ttl_seconds=30, owner="kit"))
        require(await provider.release(lease), "the first release must answer True")
        require(
            not await provider.release(lease),
            "a lease already released must answer False",
        )

    # -------------------------------------------------------------- holder
    async def test_holder_reports_owner_and_fence(
        self, provider: LeaseProvider
    ) -> None:
        name = _name()
        require(await provider.holder(name) is None, "a name never held has no holder")
        lease = _granted(
            await provider.acquire(name, ttl_seconds=30, owner="kit:owner-1")
        )
        holder = await provider.holder(name)
        if holder is None:
            raise AssertionError("a held name must have a holder")
        require(
            (holder.owner, holder.fence) == ("kit:owner-1", lease.fence),
            "holder must report the owner and the fence of the grant",
        )
        await provider.release(lease)
        require(
            await provider.holder(name) is None,
            "a released name must read as free",
        )

    # ----------------------------------------------------------- arguments
    async def test_a_ttl_below_the_minimum_raises(
        self, provider: LeaseProvider
    ) -> None:
        for ttl in (0, 1, MIN_LEASE_TTL_SECONDS - 1, -5):
            with pytest.raises(InvalidArgument):
                await provider.acquire(_name(), ttl_seconds=ttl, owner="kit")

    async def test_an_empty_or_overlong_name_raises(
        self, provider: LeaseProvider
    ) -> None:
        for name in ("", "x" * (MAX_LEASE_NAME_LENGTH + 1)):
            with pytest.raises(InvalidArgument):
                await provider.acquire(name, ttl_seconds=_TTL, owner="kit")


__all__ = ["LeaseConformance"]
