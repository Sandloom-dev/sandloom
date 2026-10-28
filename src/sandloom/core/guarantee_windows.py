"""Guarantee windows: the settings that bound an exactly-once / at-least-once
promise, checked against each other (ADR-0005, "Guarantee windows").

A dedupe record dedupes only while it lives, and a delivery is retried only
while something retries it. Each window is an ops knob on its own; the
guarantee holds only when they agree:

* ``inbox_purge_below_redelivery`` — a durable-inbox claim must outlive every
  redelivery of its event. The Streams transport keeps entries for
  ``EVENT_STREAM_RETENTION_SECONDS`` (a group recreated from ``0``, a pending
  reclaim, ``dlq-replay`` can redeliver anything still retained), so an
  ``sandloom outbox purge --inbox-days D`` with ``D`` below that retention (plus
  :data:`INBOX_RETENTION_MARGIN_SECONDS`) lets a redelivery apply its effect a
  second time. With retention ``0`` (unbounded) every inbox purge is a risk.
  Checked by the CLI (the purge window is a CLI argument, not a setting).
* ``command_dedupe_below_retry_horizon`` — a command contract's record must
  outlive the framework's automatic retries of that command. The documented
  horizon is the outbox relay's redelivery budget
  (:func:`relay_retry_horizon_seconds`): a durable consumer issuing a command
  keyed by its event keeps retrying while the relay redelivers the event.
  Outages of the broker (the relay's circuit) and operator replays
  (``sandloom outbox requeue`` / ``dlq-replay``) are NOT bounded by any setting: a
  saga retrying longer must raise ``CONTRACT_COMMAND_DEDUPE_TTL_SECONDS``.
* ``saga_horizon_beyond_command_dedupe`` — a saga command step can be retried
  or resolved until ``(step deadline, else saga deadline) +
  compensation_deadline`` after its first attempt (ADR-0008 §9): that span
  must fit ``CONTRACT_COMMAND_DEDUPE_TTL_SECONDS``, or a late retry re-runs a
  step whose record expired. Checked for every command step of every
  registered definition (``sagas=``); an ``await_event`` step (phase 2) holds
  no key open and does not count. Operator retries later than that are
  refused by ``sandloom sagas retry`` itself (the post-TTL rule).
The third window of the review — ``OUTBOX_RELAY_ENABLED=off`` in a production
app process (at-least-once delivery loses its retry) — is the accepted-risks
list's ``relay_disabled`` (``SANDLOOM_ACCEPTED_RISKS``, its own startup check that
knows the gunicorn relay-leader election), not a window here.

:func:`guarantee_window_problems` returns every violated window as a
:class:`Problem` with a stable ``risk_id``. ``create_app`` calls
:func:`enforce_guarantee_windows`: production refuses to start
(:class:`GuaranteeWindowError`) unless the risk is accepted, other
environments log a warning. A risk is accepted through ``SANDLOOM_ACCEPTED_RISKS``
(registered in :mod:`sandloom.core.config.accepted_risks`). The inbox-purge window
is refused by the CLI unless ``--force``.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sandloom.core.config import accepted_risks as _risks
from sandloom.core.effects import relay_retry_horizon_seconds

logger = logging.getLogger("sandloom.core.guarantee_windows")

#: Risk ids (stable: accepted-risk lists and dashboards refer to them).
INBOX_PURGE_BELOW_REDELIVERY = "inbox_purge_below_redelivery"
COMMAND_DEDUPE_BELOW_RETRY_HORIZON = "command_dedupe_below_retry_horizon"
SAGA_HORIZON_BEYOND_COMMAND_DEDUPE = "saga_horizon_beyond_command_dedupe"
RISK_IDS: tuple[str, ...] = (
    INBOX_PURGE_BELOW_REDELIVERY,
    COMMAND_DEDUPE_BELOW_RETRY_HORIZON,
    SAGA_HORIZON_BEYOND_COMMAND_DEDUPE,
)

#: Slack an inbox claim keeps beyond the stream retention (a group recreated
#: or a replay right at the retention edge, clock skew between Redis and the
#: database).
INBOX_RETENTION_MARGIN_SECONDS = 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class Problem:
    """One violated guarantee window."""

    risk_id: str
    message: str
    #: The settings / arguments whose values disagree.
    settings: tuple[str, ...] = ()


class GuaranteeWindowError(RuntimeError):
    """Production refuses a configuration whose guarantee windows disagree."""

    fatal_at_startup = True


def inbox_purge_problems(settings: Any, inbox_days: float) -> list[Problem]:
    """The inbox-purge window of ``sandloom outbox purge --inbox-days``."""
    retention = int(settings.event_stream_retention_seconds)
    purge_after = float(inbox_days) * 86400.0
    if retention <= 0:
        return [
            Problem(
                INBOX_PURGE_BELOW_REDELIVERY,
                "EVENT_STREAM_RETENTION_SECONDS=0 keeps stream entries without "
                "a time bound, so any redelivery horizon is unbounded: purging "
                f"inbox claims older than {inbox_days:g} day(s) lets a "
                "redelivered event apply its effect twice",
                ("EVENT_STREAM_RETENTION_SECONDS", "--inbox-days"),
            )
        ]
    needed = retention + INBOX_RETENTION_MARGIN_SECONDS
    if purge_after < needed:
        return [
            Problem(
                INBOX_PURGE_BELOW_REDELIVERY,
                f"--inbox-days {inbox_days:g} purges claims younger than the "
                f"stream retention (EVENT_STREAM_RETENTION_SECONDS={retention}) "
                f"+ {INBOX_RETENTION_MARGIN_SECONDS // 3600} h margin: a "
                "redelivery after the purge applies its effect twice — use "
                f"--inbox-days >= {needed / 86400.0:g}",
                ("EVENT_STREAM_RETENTION_SECONDS", "--inbox-days"),
            )
        ]
    return []


def saga_horizon_problems(settings: Any, sagas: Iterable[Any]) -> list[Problem]:
    """The saga window (ADR-0008 §9) of each registered definition.

    ``(step deadline, else saga deadline) + compensation_deadline <=
    CONTRACT_COMMAND_DEDUPE_TTL_SECONDS`` for every command step; the
    compensation deadline is the definition's, else
    ``SAGA_COMPENSATION_DEADLINE_SECONDS``.
    """
    definitions = list(sagas)
    if not definitions:
        return []
    ttl = int(settings.contract_command_dedupe_ttl_seconds)
    default_compensation = timedelta(
        seconds=int(settings.saga_compensation_deadline_seconds)
    )
    problems: list[Problem] = []
    for saga in definitions:
        compensation = saga.compensation_deadline or default_compensation
        for step in saga.steps:
            if getattr(step, "local", True):
                continue  # transactional: no command record to outlive
            deadline = step.deadline or saga.deadline
            horizon = deadline + compensation
            if horizon.total_seconds() <= ttl:
                continue
            problems.append(
                Problem(
                    SAGA_HORIZON_BEYOND_COMMAND_DEDUPE,
                    f"saga {getattr(saga, '_saga_type', None) or saga.name} step "
                    f"{step.name!r} can be retried or resolved up to "
                    f"{horizon.total_seconds():.0f} s after its first attempt "
                    f"(deadline {deadline.total_seconds():.0f} s "
                    f"+ compensation {compensation.total_seconds():.0f} s), beyond "
                    f"CONTRACT_COMMAND_DEDUPE_TTL_SECONDS={ttl}: a late retry "
                    "re-runs a step whose dedupe record expired — shorten the "
                    "deadlines or raise the TTL",
                    (
                        "CONTRACT_COMMAND_DEDUPE_TTL_SECONDS",
                        "SAGA_COMPENSATION_DEADLINE_SECONDS",
                    ),
                )
            )
    return problems


def guarantee_window_problems(
    settings: Any,
    *,
    inbox_days: float | None = None,
    sagas: Iterable[Any] = (),
) -> list[Problem]:
    """Every guarantee window ``settings`` violates (deterministic, no I/O).

    ``inbox_days`` adds the inbox-purge window (the CLI's argument);
    ``sagas`` the registered saga definitions (the saga horizon).
    """
    problems: list[Problem] = []
    ttl = int(settings.contract_command_dedupe_ttl_seconds)
    horizon = relay_retry_horizon_seconds(settings)
    if ttl < horizon:
        problems.append(
            Problem(
                COMMAND_DEDUPE_BELOW_RETRY_HORIZON,
                f"CONTRACT_COMMAND_DEDUPE_TTL_SECONDS={ttl} is shorter than the "
                f"outbox relay's redelivery horizon ({horizon:.0f} s: "
                "OUTBOX_RELAY_MAX_ATTEMPTS x backoff): a command retried by a "
                "redelivered event after its record expired runs twice",
                (
                    "CONTRACT_COMMAND_DEDUPE_TTL_SECONDS",
                    "OUTBOX_RELAY_MAX_ATTEMPTS",
                    "OUTBOX_RELAY_BACKOFF_BASE_SECONDS",
                    "OUTBOX_RELAY_BACKOFF_MAX_SECONDS",
                ),
            )
        )
    problems.extend(saga_horizon_problems(settings, sagas))
    if inbox_days is not None:
        problems.extend(inbox_purge_problems(settings, inbox_days))
    return problems


def accepted_risks(settings: Any) -> frozenset[str]:
    """Risk ids the configuration accepts (``SANDLOOM_ACCEPTED_RISKS``,
    :func:`sandloom.core.config.accepted_risks.effective`)."""
    return _risks.effective(settings)


def enforce_guarantee_windows(
    settings: Any,
    *,
    accepted: Collection[str] | None = None,
    log: logging.Logger | None = None,
    sagas: Iterable[Any] = (),
) -> list[Problem]:
    """Startup check: refuse in production, warn elsewhere.

    ``accepted`` defaults to :func:`accepted_risks`. Returns the problems
    that are neither accepted nor fatal (logged). Raises
    :class:`GuaranteeWindowError` in production when any remains.
    """
    taken = accepted_risks(settings) if accepted is None else frozenset(accepted)
    problems = [
        p
        for p in guarantee_window_problems(settings, sagas=sagas)
        if p.risk_id not in taken
    ]
    if not problems:
        return []
    if settings.is_prod:
        raise GuaranteeWindowError(_render(problems))
    for problem in problems:
        (log or logger).warning(
            "guarantee window %s: %s", problem.risk_id, problem.message
        )
    return problems


def _render(problems: Iterable[Problem]) -> str:
    return "; ".join(f"[{p.risk_id}] {p.message}" for p in problems)


__all__ = [
    "COMMAND_DEDUPE_BELOW_RETRY_HORIZON",
    "INBOX_PURGE_BELOW_REDELIVERY",
    "INBOX_RETENTION_MARGIN_SECONDS",
    "RISK_IDS",
    "SAGA_HORIZON_BEYOND_COMMAND_DEDUPE",
    "GuaranteeWindowError",
    "Problem",
    "accepted_risks",
    "enforce_guarantee_windows",
    "guarantee_window_problems",
    "inbox_purge_problems",
    "saga_horizon_problems",
]
