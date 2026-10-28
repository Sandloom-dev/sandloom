"""Operation-intent mode (spec §4.6): billing checkout and portal.

A route declared ``operation_intent(reason=)`` has NO request unit
(``db()`` outside an ``db_session()`` block raises SANDLOOM-R002) and runs its
protocol as SEQUENTIAL short units around an external (PSP) call:

* **units** of the route (``db_session()`` blocks) never overlap: one may not
  open while another is open (SANDLOOM-R008);
* :func:`external_call` — every PSP call runs inside it. The exclusion is
  two-way and lasts for the whole call: it refuses to start while a unit of
  the request is open, waits for the request's contract calls already
  running in their provider units (refused from inside one) and releases a
  held contract read-scope connection first, and while it is active no
  unit, contract call or connection checkout may start in the request, in
  any task (SANDLOOM-R008); a REMOTE contract call of the request whose
  provider never answered an attempt (timeout, transport failure) may still
  run its unit there, so it refuses to open at all (fail closed, ``503``
  :class:`RemoteCallInDoubtError`, SANDLOOM-R008: the client retries the request
  with its ``Idempotency-Key``);
* :func:`release_check` — the final re-read before a bearer URL is released:
  refused while a unit is open; for an organization account the authority
  re-read is REQUIRED (SANDLOOM-R012 otherwise); for its whole duration it holds
  the same two-way exclusion, except the authority callback's own
  READ-contract calls (from the callback's context only; commands refused);
  then the subject check (401) — failing closed on any error;
* the **release rule** (checked when the route answers 2xx; otherwise the
  body is discarded and the request fails with SANDLOOM-R012): either *issue* — at
  least one external call, ``release_check()`` after the last one, then
  exactly one unit committing exactly one write statement — or *issued retry*
  — no external call, ``release_check()`` after the last committed unit, no
  unit committing a write after it.

Hence no billing transaction, connection or row lock is open across a PSP
call or the final re-read (G-BILLING-19, proved by construction: there is no
request unit, units never overlap, and nothing opens inside the windows but
the re-read's own read-contract calls, which hold no billing connection).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from fastapi import HTTPException

from sandloom.core.unit_of_work.effects import UnitOfWorkRequiredError
from sandloom.core.unit_of_work.request import (
    UnitRuleError,
    current_request_scope,
)


class IntentReleaseError(RuntimeError):
    """An operation-intent route answered 2xx outside the release rule, or
    called ``release_check`` without the authority re-read an organization
    account needs (SANDLOOM-R012)."""

    sandloom_code = "SANDLOOM-R012"


class RemoteCallInDoubtError(HTTPException):
    """An intent window refused to open: a remote contract call of the
    request got no answer from its provider for an attempt (a timeout, a
    transport failure), so its provider unit may still be running (``503``,
    SANDLOOM-R008). Retry the request with the same ``Idempotency-Key``: the
    command replays (deduped) or waits for the running one."""

    sandloom_code = "SANDLOOM-R008"

    def __init__(self, what: str, calls: list[str]) -> None:
        super().__init__(
            status_code=503,
            detail=(
                f"{what} refused: a remote contract call of this request "
                f"({', '.join(sorted(set(calls)))}) got no answer from its "
                "provider (a timeout or a transport failure), so its provider "
                "unit may still be running and no external call or release "
                "check may run beside it; retry the request with the same "
                "Idempotency-Key (SANDLOOM-R008)"
            ),
            headers={"Retry-After": "1"},
        )


@dataclass(frozen=True)
class ResolvedAccount:
    """The billing account a release check is for (spec §2.3, §4.6, §7)."""

    kind: Literal["user", "org"]
    id: UUID


#: Set (True) inside ``release_check``'s authority callback context only.
_in_authority: ContextVar[bool] = ContextVar("sandloom_intent_authority", default=False)
#: Set (True) inside a contract call's provider unit (its own context): a
#: window entered from there cannot wait for the unit it runs in.
_in_provider: ContextVar[bool] = ContextVar("sandloom_intent_provider", default=False)


class IntentState:
    """The intent bookkeeping of one request (``RequestScope.intent``)."""

    __slots__ = (
        "events",
        "external_active",
        "in_doubt",
        "open_units",
        "provider_idle",
        "provider_units",
        "read_scope_release",
        "release_active",
    )

    def __init__(self) -> None:
        #: ``("external", name)`` / ``("release", None)`` / ``("commit", writes)``
        self.events: list[tuple[str, Any]] = []
        self.open_units: list[Any] = []
        self.external_active = 0
        self.release_active = False
        #: releases the request's contract read-scope connection, if any
        self.read_scope_release: Callable[[], Awaitable[None]] | None = None
        #: contract calls of the request running in their provider unit (or
        #: on the read scope): a window waits for them before it opens
        self.provider_units = 0
        #: set when ``provider_units`` drops to 0 (a window is waiting)
        self.provider_idle: asyncio.Event | None = None
        #: remote calls with an unanswered attempt: their provider unit may
        #: still be running (the transport finishing proves nothing)
        self.in_doubt: list[str] = []

    # -- the windows ---------------------------------------------------------
    def _window(self) -> str | None:
        if self.external_active:
            return "an external_call()"
        if self.release_active and not _in_authority.get():
            return "release_check()"
        return None

    def refuse_request_unit(self) -> None:
        raise UnitOfWorkRequiredError(
            "db() on an operation-intent route: it has no request unit — open a "
            "short unit with `async with db_session() as session:` (SANDLOOM-R002)"
        )

    def on_unit_open(self, session: Any) -> None:
        """A unit of the route opens (a ``db_session()`` block)."""
        window = self._window() or ("release_check()" if self.release_active else None)
        if window is not None:
            raise UnitRuleError(
                f"a unit opened while {window} is active: no transaction may span "
                "the external call or the final re-read (SANDLOOM-R008)"
            )
        if self.open_units:
            raise UnitRuleError(
                "a unit opened while another unit of this operation-intent route "
                "is open: units are sequential — finish the open one first "
                "(SANDLOOM-R008)"
            )
        self.open_units.append(session)

    def on_unit_close(self, session: Any) -> None:
        if session in self.open_units:
            self.open_units.remove(session)

    def on_unit_commit(self, writes: int) -> None:
        self.events.append(("commit", writes))

    def enter_contract(self, readonly: bool) -> Token[bool]:
        """A contract call starts (local; its own unit / read scope): refused
        inside a window (:meth:`on_contract_call`), else counted until
        :meth:`leave_contract`, so a window entered meanwhile waits for it
        (G-UNIT-02 / G-BILLING-19: a window never runs beside a provider
        transaction of the request)."""
        self.on_contract_call(readonly)
        self.provider_units += 1
        return _in_provider.set(True)

    def leave_contract(self, token: Token[bool]) -> None:
        _in_provider.reset(token)
        self.provider_units -= 1
        idle = self.provider_idle
        if not self.provider_units and idle is not None:
            self.provider_idle = None
            idle.set()

    def note_in_doubt(self, call: str) -> None:
        """A remote call's attempt got no answer from its provider: it may
        still be running there (the RPC proxy)."""
        self.in_doubt.append(call)

    def refuse_in_doubt(self, what: str) -> None:
        """No window opens while a remote call of the request may still run
        in its provider unit (G-UNIT-02 / G-BILLING-19, fail closed)."""
        if self.in_doubt:
            raise RemoteCallInDoubtError(what, self.in_doubt)

    async def wait_provider_units(self, what: str) -> None:
        """Wait (the window already open: no new call starts) for the
        request's running contract calls to finish; refused from inside one
        (it cannot finish first) (SANDLOOM-R008)."""
        while self.provider_units:
            if _in_provider.get():
                raise UnitRuleError(
                    f"{what} inside a running contract call: its provider unit "
                    "cannot finish first (SANDLOOM-R008)"
                )
            idle = self.provider_idle
            if idle is None:
                idle = self.provider_idle = asyncio.Event()
            await idle.wait()

    def on_contract_call(self, readonly: bool) -> None:
        """A contract call starts (local; its own unit / read scope)."""
        if self.external_active:
            raise UnitRuleError(
                "a contract call while an external_call() is active (SANDLOOM-R008)"
            )
        if self.release_active and not (_in_authority.get() and readonly):
            raise UnitRuleError(
                "only the authority callback's READ-contract calls may run "
                "during release_check() (a command, or a call from another "
                "context, is refused) (SANDLOOM-R008)"
            )

    def _admitted_provider(self) -> bool:
        """This context runs a contract call admitted before the window (one
        the window is waiting for)."""
        return bool(self.provider_units) and _in_provider.get()

    def check_checkout(self) -> None:
        """A connection is checked out (the kernel session's first statement).

        A contract call admitted before the window opened finishes in its
        provider unit (the window waits for it before the external call or
        the re-read runs): its statements are not refused."""
        window = self._window()
        if window is not None and not self._admitted_provider():
            raise UnitRuleError(
                f"a connection checkout while {window} is active (SANDLOOM-R008)"
            )

    def check_write_allowed(self) -> None:
        window = self._window()
        if window is not None and not self._admitted_provider():
            raise UnitRuleError(f"a write while {window} is active (SANDLOOM-R008)")

    # -- the release rule ----------------------------------------------------
    def release_problem(self) -> str | None:
        """Why a 2xx violates the release rule (``None``: it matches a case)."""
        events = self.events
        externals = [i for i, (kind, _) in enumerate(events) if kind == "external"]
        releases = [i for i, (kind, _) in enumerate(events) if kind == "release"]
        if not releases:
            return "no release_check() before the 2xx"
        last_release = releases[-1]
        after = [w for kind, w in events[last_release + 1 :] if kind == "commit"]
        if externals:
            if last_release < externals[-1]:
                return "release_check() did not run after the last external_call()"
            if len(after) != 1 or after[0] != 1:
                return (
                    "after release_check() exactly one unit must commit exactly one "
                    f"write statement (the issue); saw commits with {after} writes"
                )
            return None
        # the issued retry: the check follows the last committed unit and no
        # unit commits after it (a fortiori no write)
        if after:
            return (
                "with no external call (the issued retry) release_check() must "
                "follow the last committed unit; a unit committed after it"
            )
        return None


def _intent() -> IntentState | None:
    """The request's intent state; ``None`` outside any request (a script, a
    test driving the service directly: no response to release, no subject)."""
    scope = current_request_scope()
    if scope is None:
        return None
    intent: IntentState | None = getattr(scope, "intent", None)
    if intent is None:
        raise UnitRuleError(
            "external_call() / release_check() run only on a route declared "
            "operation_intent(reason=...) (SANDLOOM-R008)"
        )
    return intent


@asynccontextmanager
async def external_call(name: str) -> AsyncIterator[None]:
    """Run one external (PSP) call with no unit of the request open (§4.6).

    Refused (SANDLOOM-R008) while a unit of the request is open; a held contract
    read-scope connection is released first. While it is active no unit,
    contract call or connection checkout may start in the request.
    """
    intent = _intent()
    if intent is None:
        yield
        return
    if intent.open_units or intent.release_active or intent.external_active:
        raise UnitRuleError(
            f"external_call({name!r}) entered while a unit is open or another "
            "window is active: finish the unit first (SANDLOOM-R008)"
        )
    # The window opens atomically with the check (no await in between): from
    # here no unit, contract call or checkout may start — a child task racing
    # the read-scope release below is refused, not admitted.
    intent.external_active += 1
    try:
        await intent.wait_provider_units(f"external_call({name!r})")
        intent.refuse_in_doubt(f"external_call({name!r})")
        if intent.read_scope_release is not None:
            await intent.read_scope_release()
        _no_open_units(intent, f"external_call({name!r})")
        intent.events.append(("external", name))
        yield
    finally:
        intent.external_active -= 1


def _no_open_units(intent: IntentState, what: str) -> None:
    """Re-verify after the read-scope drain that no unit of the route is open."""
    if intent.open_units:
        raise UnitRuleError(f"{what}: a unit of the request is open (SANDLOOM-R008)")


async def release_check(
    account: ResolvedAccount,
    *,
    authority: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """The final re-read before an intent route releases a bearer value.

    ``authority`` (the authority re-read, e.g. billing's
    ``reverify_authority``) is REQUIRED for an organization account
    (SANDLOOM-R012). Then the request subject's session is checked live (401
    revoked / blocked; any error fails closed). For its whole duration no
    unit, command or connection checkout may start in the request, except the
    authority callback's own read-contract calls (SANDLOOM-R008).
    """
    intent = _intent()
    if account.kind == "org" and authority is None:
        raise IntentReleaseError(
            "release_check() for an organization account needs authority= (the "
            "authority re-read, e.g. reverify_authority) (SANDLOOM-R012)"
        )
    if intent is None:
        if authority is not None:
            await authority()
        return
    if intent.open_units or intent.external_active or intent.release_active:
        raise UnitRuleError(
            "release_check() entered while a unit of the request is open (or an "
            "external call runs): finish it first (SANDLOOM-R008)"
        )
    scope = current_request_scope()
    # The window opens atomically with the check (see external_call).
    intent.release_active = True
    try:
        await intent.wait_provider_units("release_check()")
        intent.refuse_in_doubt("release_check()")
        if intent.read_scope_release is not None:
            await intent.read_scope_release()
        _no_open_units(intent, "release_check()")
        if authority is not None:
            token = _in_authority.set(True)
            try:
                await authority()
            finally:
                _in_authority.reset(token)
            if intent.read_scope_release is not None:
                await intent.read_scope_release()
        # the authority's own remote reads, too: none may still be running
        intent.refuse_in_doubt("release_check()")
        check = getattr(scope, "check", None)
        if check is not None:
            await check()
    finally:
        intent.release_active = False
    intent.events.append(("release", None))


__all__ = [
    "IntentReleaseError",
    "IntentState",
    "RemoteCallInDoubtError",
    "ResolvedAccount",
    "external_call",
    "release_check",
]
