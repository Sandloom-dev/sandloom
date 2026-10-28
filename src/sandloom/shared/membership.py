"""Membership state + the ordering rule for ``tenancy.member_*.v1`` consumers.

Member events (``tenancy.member_added.v1`` / ``member_role_changed.v1`` /
``member_removed.v1``) are delivered at-least-once with NO ordering guarantee.
Since tenancy 0005 they carry a per-``(organization_id, user_id)`` ``version``
— but ``version`` is OPTIONAL within ``.v1``: during a rolling upgrade an OLD
tenancy instance still publishes version-less events (and does not bump the
version when it changes a membership). A consumer that kept the last applied
version must therefore decide per event:

* ``version`` present -> **apply** iff it is newer than the stored version
  (or nothing is stored); otherwise **skip** (duplicate / out of order);
* ``version`` absent -> **refresh**: the event's position is unknown, so it is
  neither applied blindly (it may be OLDER than what is stored and would roll
  the state back) nor dropped (it may be the LATEST change, made by an old
  instance that bumps no version). The consumer re-reads the owner's current
  state with ``tenancy.membership.v1.get_member_state(org, user)`` — role AND
  version read in one statement — and stores it. Any event committed before
  that read carries a version ``<=`` the one read, so it is skipped afterwards
  and can no longer regress the refreshed state.

:func:`member_event_action` is that decision. ``MemberState`` is tenancy's
contract DTO; callers hold its generated copy
(``sandloom.shared.contracts.tenancy_dto``, written by ``sandloom generate``).
This module is hand-written consumer logic, not a DTO.
"""

from __future__ import annotations

from typing import Any, Literal

from sandloom.shared.contracts.tenancy_dto import MemberState

MemberEventAction = Literal["apply", "skip", "refresh"]


def member_event_version(payload: dict[str, Any]) -> int | None:
    """The event's ``version``, or ``None`` for a version-less (old) event."""
    raw = payload.get("version")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        return None
    return raw


def member_event_action(
    payload: dict[str, Any], stored_version: int | None
) -> MemberEventAction:
    """``"apply"`` / ``"skip"`` / ``"refresh"`` for one delivered member event.

    ``stored_version`` is the version the consumer last stored for the pair
    (``None`` when it stores nothing yet).
    """
    incoming = member_event_version(payload)
    if incoming is None:
        return "refresh"
    if stored_version is None or incoming > stored_version:
        return "apply"
    return "skip"


def refreshed_state_wins(state: MemberState, stored_version: int | None) -> bool:
    """Store a refreshed :class:`MemberState` unless it is older than the
    stored one (a concurrent, newer versioned event was applied meanwhile)."""
    return stored_version is None or state.version >= stored_version


__all__ = [
    "MemberEventAction",
    "member_event_action",
    "member_event_version",
    "refreshed_state_wins",
]
