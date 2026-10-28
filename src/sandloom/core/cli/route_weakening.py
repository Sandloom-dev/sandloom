"""Which route policy changes WEAKEN a route (spec §5.8, G-ROUTE-04).

A module's ``routes.snapshot.json`` is the approval of each route's exposure
and policy. ``sandloom test`` regenerates it in dev, but never over a weaker
policy: :func:`weakenings` names every change below, and the snapshot is only
rewritten once the change is approved explicitly with ``sandloom routes snapshot
--write`` (whose diff is the reviewed record of the approval):

* authentication removed (``authenticated`` -> ``service`` / ``public``), or
  the route made ``public``;
* a risk kind removed;
* idempotency removed, no longer fail-closed, or no longer durable;
* the session check weakened (``none`` < re-check if a unit writes <
  re-check before commit < re-check every unit / release_check);
* the automatic audit dropped;
* step-up removed or its maximum age raised;
* a custom authorization dependency removed.

Stronger or neutral changes (an added route, a new risk, a stricter check)
are written as usual; a removed route is not a weakening.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

_AUTH_RANK = {"public": 0, "service": 1, "authenticated": 2}
_SESSION_RANK = {
    "none": 0,
    "live": 1,
    "live, re-check if a unit writes": 1,
    "live, re-check before commit": 2,
    "live, re-check every unit": 3,
    "live, release_check": 3,
}

Policy = Mapping[str, Any]


def _auth(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    before = _AUTH_RANK.get(str(old.get("auth")), 0)
    after = _AUTH_RANK.get(str(new.get("auth")), 0)
    return ["authentication was removed"] if after < before else []


def _public(old: Policy, new: Policy) -> list[str]:
    reason = new.get("public_reason")
    if reason is not None and old.get("public_reason") is None:
        return [f"the route became public ({reason})"]
    return []


def _risk(old: Policy, new: Policy) -> list[str]:
    kept = set(new.get("risk") or {})
    return [
        f"risk {kind!r} was removed"
        for kind in old.get("risk") or {}
        if kind not in kept
    ]


@dataclass(frozen=True)
class IdempotencyProps:
    """What an ``idempotency`` snapshot spelling guarantees (G-ROUTE-04).

    * ``present`` — a retry with the same key is deduped at all;
    * ``fail_closed`` — a store outage refuses the request (``503``) instead
      of letting a duplicate through;
    * ``durable`` — the record commits with the effect (a retry after an
      ambiguous COMMIT never re-runs it).
    """

    present: bool
    fail_closed: bool
    durable: bool


_NO_IDEMPOTENCY = IdempotencyProps(present=False, fail_closed=False, durable=False)
#: An unrecognised spelling: assumed to guarantee everything when it is the
#: committed one and nothing when it is the new one, so any change from or to
#: it is flagged (never silently accepted).
_UNKNOWN_OLD = IdempotencyProps(present=True, fail_closed=True, durable=True)


def idempotency_props(spelling: str | None) -> IdempotencyProps | None:
    """The :class:`IdempotencyProps` of every spelling
    :func:`sandloom.core.route_snapshots._idempotency` writes, ``None`` for an
    unknown one:

    * ``none`` / ``none (<reason>)`` — nothing;
    * ``automatic S`` / ``automatic B (<reason>)`` — the automatic guard: a
      durable record of the request unit, fail-closed;
    * ``explicit <durable|cache|in-flight>[, fail-closed]`` — an explicit
      ``idempotency_guard``: only the ``durable`` kind commits its record with
      the effect; ``fail-closed`` as spelled.
    """
    text = str(spelling or "none").strip()
    if text == "none" or text.startswith("none ("):
        return _NO_IDEMPOTENCY
    if text == "automatic S" or (
        text.startswith("automatic B (") and text.endswith(")")
    ):
        return IdempotencyProps(present=True, fail_closed=True, durable=True)
    if text.startswith("explicit "):
        kind, _, suffix = text.removeprefix("explicit ").partition(", ")
        if kind in {"durable", "cache", "in-flight"} and suffix in {"", "fail-closed"}:
            return IdempotencyProps(
                present=True,
                fail_closed=suffix == "fail-closed",
                durable=kind == "durable",
            )
    return None


def _idempotency(old: Policy, new: Policy) -> list[str]:
    before = str(old.get("idempotency") or "none")
    after = str(new.get("idempotency") or "none")
    if before == after:
        return []
    change = f"({before} -> {after})"
    was = idempotency_props(before) or _UNKNOWN_OLD
    now = idempotency_props(after)
    if now is None:
        return [f"idempotency changed to an unknown mode {change}"]
    if was.present and not now.present:
        return [f"idempotency was removed {change}"]
    out = []
    if was.fail_closed and not now.fail_closed:
        out.append(f"idempotency no longer fails closed {change}")
    if was.durable and not now.durable:
        out.append(f"idempotency is no longer durable {change}")
    return out


def _session(old: Policy, new: Policy) -> list[str]:
    before, after = str(old.get("session") or "none"), str(new.get("session") or "none")
    if _SESSION_RANK.get(after, 0) < _SESSION_RANK.get(before, 0):
        return [f"the session check was weakened ({before} -> {after})"]
    return []


def _audit(old: Policy, new: Policy) -> list[str]:
    if old.get("audit") and not new.get("audit"):
        return ["the automatic audit was dropped"]
    return []


def _step_up(old: Policy, new: Policy) -> list[str]:
    before, after = old.get("step_up_max_age_s"), new.get("step_up_max_age_s")
    if before is None:
        return []
    if after is None:
        return ["step-up was removed"]
    if after > before:
        return [f"step-up was relaxed ({before} s -> {after} s)"]
    return []


def _authz(old: Policy, new: Policy) -> list[str]:
    kept = set(new.get("authz") or ())
    return [
        f"authorization {name} was removed"
        for name in old.get("authz") or ()
        if name not in kept
    ]


_POLICY_CHECKS: tuple[Callable[[Policy, Policy], list[str]], ...] = (
    _public,
    _risk,
    _idempotency,
    _session,
    _audit,
    _step_up,
    _authz,
)


def entry_weakenings(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    """How the snapshot entry ``new`` weakens ``old`` (same method and path)."""
    old_policy, new_policy = old.get("policy") or {}, new.get("policy") or {}
    out = _auth(old, new)
    for check in _POLICY_CHECKS:
        out += check(old_policy, new_policy)
    return out


def weakenings(old: str | None, new: Mapping[str, Any]) -> list[str]:
    """``"<METHOD> <path>: <what>"`` for every route of the snapshot document
    ``new`` whose policy is weaker than in the committed text ``old``."""
    if not old:
        return []
    try:
        before_doc = json.loads(old)
    except ValueError:
        return []
    before = {(e["method"], e["path"]): e for e in before_doc.get("routes", [])}
    problems = []
    for entry in new.get("routes", []):
        previous = before.get((entry["method"], entry["path"]))
        if previous is not None:
            problems += [
                f"{entry['method']} {entry['path']}: {what}"
                for what in entry_weakenings(previous, entry)
            ]
    return problems


__all__ = ["IdempotencyProps", "entry_weakenings", "idempotency_props", "weakenings"]
