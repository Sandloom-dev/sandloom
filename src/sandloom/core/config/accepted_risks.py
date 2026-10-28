"""Accepted risks: the ONE place a deployment admits a weaker guarantee.

``SANDLOOM_ACCEPTED_RISKS`` is a JSON list (or comma-separated string) of the
stable risk ids registered in :data:`RISKS`. Each id names one weakening that
the framework otherwise refuses in production (a fake PSP, a volatile event
bus, argon2 parameters below the OWASP floor, …): the check that would refuse
it consults :func:`accepts` instead of a boolean of its own, so the full list
of weakenings a deployment runs with is one reviewable setting — the model is
Django's ``SILENCED_SYSTEM_CHECKS``.

* An unknown id fails startup (the ``Settings`` validator): a typo never
  silently accepts nothing — or the wrong thing.
* Every accepted risk is logged as a WARNING at every start, counted in
  ``sandloom_accepted_risk_active{risk}`` (and ``sandloom_config_override_active`` with
  ``kind="accepted_risk"``), listed by ``/diagnostics/core``
  (``configuration.accepted_risks``) and by ``sandloom doctor``
  (:mod:`sandloom.core.config.tiers` reports them as overrides).
* There is no other spelling: the former per-check booleans
  (``BILLING_ALLOW_FAKE_PSP_IN_PROD``, …) are removed settings
  (``config_tiers.REMOVED``) and fail startup when set.

Pure data + pure functions of a settings-like object: no I/O and no import of
``sandloom.core.config.settings`` (its validator uses this module).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Risk:
    """One stable, documented weakening a deployment may accept."""

    id: str
    #: what accepting it weakens (one sentence, operator-facing).
    weakens: str
    #: which setting / check consults it.
    consulted_by: str
    #: profiles (``sandloom.core.config.tiers.PROFILES``) that refuse it even when
    #: accepted (the production profile's startup check).
    refused: frozenset[str] = frozenset()
    #: it also weakens something outside prod (else accepting it there is
    #: inert and is not reported as an override outside prod).
    outside_prod: bool = False


RISKS: tuple[Risk, ...] = (
    Risk(
        "fake_psp_in_prod",
        "BILLING_PROVIDER=fake with ENVIRONMENT=prod: fake checkout sessions, "
        "no real payment (demo stacks).",
        "Settings validator (BILLING_PROVIDER) and billing's critical startup "
        "check (sandloom_billing.psp.psp_configuration_error)",
    ),
    Risk(
        "log_notifier_in_prod",
        "The log notifier advertises delivery channels in prod: mail and SMS "
        "nobody receives.",
        "log notifier driver features (sandloom.core.drivers.log_notifier)",
    ),
    Risk(
        "local_storage_in_prod",
        "Local-filesystem object storage in prod: no replication, lost with "
        "the container.",
        "local object-store driver (sandloom.core.drivers.local_objectstore)",
    ),
    Risk(
        "volatile_event_bus",
        "A volatile event bus (memory / Redis Pub/Sub): in prod the driver is "
        "admitted and events can be lost; outside prod durable consumers may "
        "run on Pub/Sub (lost or duplicated deliveries).",
        "event-bus driver (sandloom.core.drivers.event_bus_driver) and the "
        "consumer runtime (sandloom.core.consumers.callbacks)",
        outside_prod=True,
    ),
    Risk(
        "unversioned_ddl_repair",
        "sandloom migrate ensure-outbox --force --apply in prod: DDL outside the "
        "versioned migration chain, no drift detection.",
        "sandloom migrate ensure-outbox (sandloom.core.cli.migrate)",
    ),
    Risk(
        "weak_password_hash",
        "argon2id parameters below the OWASP minimum in prod: stolen password "
        "hashes are cheaper to crack.",
        "Settings validator (PASSWORD_HASH_*) and the startup warning",
    ),
    Risk(
        "shared_hs_secret_split",
        "A split prod deployment keeps a shared HS* JWT secret: every service "
        "holding it can mint a token for any user.",
        (
            "startup token-signing role check "
            "(sandloom.core.auth.tokens.token_signing_problem)"
        ),
        refused=frozenset({"prod_strict"}),
    ),
    Risk(
        "relay_disabled",
        "OUTBOX_RELAY_ENABLED=off in a prod app process: no relay retries the "
        "outbox, so delivery degrades from at-least-once to the post-commit "
        "dispatch alone (a failed dispatch is never retried).",
        "app startup check (sandloom.core.app_factory.outbox_relay_problem)",
    ),
    Risk(
        "command_dedupe_below_retry_horizon",
        "CONTRACT_COMMAND_DEDUPE_TTL_SECONDS below the outbox relay's "
        "redelivery horizon in prod: a command retried after its dedupe record "
        "expired runs a second time.",
        "app startup check (sandloom.core.guarantee_windows.enforce_guarantee_windows)",
    ),
    Risk(
        "saga_horizon_beyond_command_dedupe",
        "A saga command step can be retried or resolved later than "
        "CONTRACT_COMMAND_DEDUPE_TTL_SECONDS after its first attempt in prod: "
        "a late retry after the dedupe record expired runs the step twice.",
        "app startup check (sandloom.core.guarantee_windows.enforce_guarantee_windows)",
    ),
)

#: Every registered id.
RISK_IDS: frozenset[str] = frozenset(r.id for r in RISKS)
_BY_ID: dict[str, Risk] = {r.id: r for r in RISKS}


def risk(risk_id: str) -> Risk:
    """The registered :class:`Risk` (``KeyError`` for an unregistered id)."""
    return _BY_ID[risk_id]


def unknown_ids(ids: Iterable[str]) -> list[str]:
    return sorted({str(i) for i in ids} - RISK_IDS)


def unknown_ids_message(ids: Iterable[str]) -> str:
    return (
        f"SANDLOOM_ACCEPTED_RISKS names unknown risk id(s) {unknown_ids(ids)}; "
        f"known: {sorted(RISK_IDS)} (docs/reference/configuration-tiers.md)"
    )


def _is_prod(cfg: Any) -> bool:
    return bool(getattr(cfg, "is_prod", False))


def effective(cfg: Any) -> frozenset[str]:
    """Every risk id ``cfg`` accepts (``SANDLOOM_ACCEPTED_RISKS``)."""
    return frozenset(getattr(cfg, "sandloom_accepted_risks", None) or ()) & RISK_IDS


def accepts(cfg: Any, risk_id: str) -> bool:
    """``cfg`` accepts ``risk_id`` (``KeyError`` for an unregistered id)."""
    item = _BY_ID[risk_id]
    return item.id in (getattr(cfg, "sandloom_accepted_risks", None) or ())


def in_effect(cfg: Any, risk_id: str) -> bool:
    """Accepting ``risk_id`` changes behaviour in ``cfg``'s environment."""
    return _is_prod(cfg) or _BY_ID[risk_id].outside_prod


def diagnostics(cfg: Any) -> list[dict[str, object]]:
    """``/diagnostics/core`` rows: one per accepted risk."""
    return [
        {
            "risk": risk_id,
            "weakens": _BY_ID[risk_id].weakens,
            "consulted_by": _BY_ID[risk_id].consulted_by,
            "in_effect": in_effect(cfg, risk_id),
        }
        for risk_id in sorted(effective(cfg))
    ]


__all__ = [
    "RISKS",
    "RISK_IDS",
    "Risk",
    "accepts",
    "diagnostics",
    "effective",
    "in_effect",
    "risk",
    "unknown_ids",
    "unknown_ids_message",
]
