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
* Kernel ids are flat (``relay_disabled``). A module's id is
  ``<module>.<name>`` (``billing.fake_psp_in_prod``), declared by the module
  (``Risk`` in its settings model; ADR-0015 decision 4, owner answers #27,
  #28). The ``Settings`` validator accepts the form only; the loader checks
  the namespace once the modules are registered (:func:`classify_module_risks`):
  a loaded module's id is checked against its declaration, a module known in
  the deployment but not loaded here makes the id inert ("not applicable
  here"), any other namespace is refused. A flat id that is not a kernel id is
  refused, naming the namespaced replacement where the module is installed.
  There is no compatibility alias.
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

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
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
        "experimental_capability",
        "An experimental capability in prod: its contract may change in any release.",
        "build_infra_registry (sandloom.core.drivers.bootstrap)",
    ),
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
        "log_links_in_prod",
        "NOTIFIER_LOG_LINKS=true in prod: password-reset and verification links "
        "land in the logs, where anyone who reads them can use them.",
        "Settings validator (NOTIFIER_LOG_LINKS)",
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
        "development_database_in_prod",
        "A database for development and tests (SQLite) in prod: one file on "
        "one host, no row security, no replication, its schema from the "
        "models with no migration path.",
        "startup configuration check "
        "(sandloom.core.app_factory.development_database_config_problem)",
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


#: A module's risk id: ``<module>.<name>``, both lower-case identifiers.
MODULE_RISK_ID = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


def is_module_id(risk_id: str) -> bool:
    """A well-formed ``<module>.<name>`` id (not necessarily a declared one)."""
    return MODULE_RISK_ID.match(str(risk_id)) is not None


def unknown_ids(ids: Iterable[str]) -> list[str]:
    """The ids that are neither a kernel id nor a well-formed module id.

    A well-formed module id is checked after the modules load
    (:func:`classify_module_risks`), never here: the kernel does not know the
    modules when its ``Settings`` validates.
    """
    given = {str(i) for i in ids}
    return sorted(given - RISK_IDS - {i for i in given if is_module_id(i)})


def unknown_ids_message(
    ids: Iterable[str], replacements: Mapping[str, str] | None = None
) -> str:
    """The refusal for the ids :func:`unknown_ids` finds in ``ids``.

    ``replacements`` maps a flat id to the namespaced id an installed module
    now declares for it (:func:`replacement_ids`)."""
    given = list(ids)
    bad = unknown_ids(given)
    message = (
        f"SANDLOOM_ACCEPTED_RISKS names unknown risk id(s) {bad}; "
        f"known: {sorted(RISK_IDS)} (docs/reference/configuration-tiers.md)"
    )
    moved = {i: r for i, r in (replacements or {}).items() if i in bad}
    if moved:
        message += "; " + ", ".join(
            f"{old!r} is now {new!r}" for old, new in sorted(moved.items())
        )
    if [i for i in bad if i not in moved]:
        message += (
            "; a module's risk id is <module>.<name> (no flat spelling, no "
            "alias: rename it once, see the upgrade notes in the CHANGELOG)"
        )
    return message


def replacement_ids(
    ids: Iterable[str], declared: Mapping[str, Sequence[Risk]]
) -> dict[str, str]:
    """``{flat id: <module>.<id>}`` for each flat id of ``ids`` that exactly
    one installed module declares (``declared``: module name -> its risks)."""
    out: dict[str, str] = {}
    for flat in unknown_ids(ids):
        if "." in flat:
            continue
        owners = sorted(
            m for m, risks in declared.items() if any(r.id == flat for r in risks)
        )
        if len(owners) == 1:
            out[flat] = f"{owners[0]}.{flat}"
    return out


@dataclass(frozen=True, slots=True)
class ModuleRiskReport:
    """The module-namespaced ids of ``SANDLOOM_ACCEPTED_RISKS`` in one process."""

    #: ids a loaded module declares: ``(<module>.<name>, its Risk)``.
    active: tuple[tuple[str, Risk], ...] = ()
    #: ids of a module known in the deployment but not loaded here: inert,
    #: listed as "not applicable here".
    inert: tuple[str, ...] = ()
    #: refusals: the id of a loaded module that declares no such risk, or of
    #: a namespace nobody in this deployment knows.
    problems: tuple[str, ...] = ()


def classify_module_risks(
    accepted: Iterable[str],
    *,
    loaded: Mapping[str, Sequence[Risk]],
    known: Callable[[], Iterable[str]],
) -> ModuleRiskReport:
    """Apply the namespace rule (owner answer #27) to the module ids of ``accepted``.

    ``loaded``: every loaded module -> the risks it declares (a module with no
    settings maps to ``()``). ``known``: asked once, only when an id names a
    module that is not loaded, for the module names known in the deployment
    (installed entry points, ``SANDLOOM_MODULES``). Kernel ids are not
    looked at.
    """
    active: list[tuple[str, Risk]] = []
    inert: list[str] = []
    problems: list[str] = []
    known_names: set[str] | None = None
    for risk_id in sorted({str(i) for i in accepted if is_module_id(i)}):
        ns, _, local = risk_id.partition(".")
        if ns in loaded:
            declared = {r.id.removeprefix(f"{ns}."): r for r in loaded[ns]}
            if local in declared:
                active.append((risk_id, replace(declared[local], id=risk_id)))
            else:
                problems.append(
                    f"SANDLOOM_ACCEPTED_RISKS names {risk_id!r}, but module {ns} "
                    f"declares no risk {local!r} (declared: "
                    f"{sorted(f'{ns}.{d}' for d in declared)})"
                )
            continue
        if known_names is None:
            known_names = {str(n) for n in known()}
        if ns in known_names:
            inert.append(risk_id)
        else:
            problems.append(
                f"SANDLOOM_ACCEPTED_RISKS names {risk_id!r}, but {ns!r} is not a "
                "module installed, loaded or listed in SANDLOOM_MODULES in this "
                f"deployment (known: {sorted(known_names)}): a typo in a module or "
                "risk name is refused, never ignored"
            )
    return ModuleRiskReport(tuple(active), tuple(inert), tuple(problems))


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


def diagnostics(
    cfg: Any, module_risks: ModuleRiskReport | None = None
) -> list[dict[str, object]]:
    """``/diagnostics/core`` rows: one per accepted risk.

    ``module_risks``: the loader's classification of the module-namespaced ids
    (``core.module_settings.risk_report``): a loaded module's id is a row like
    a kernel one, an id of a module not loaded here is listed as not
    applicable (``"applicable": false``)."""
    rows: list[dict[str, object]] = [
        {
            "risk": risk_id,
            "weakens": _BY_ID[risk_id].weakens,
            "consulted_by": _BY_ID[risk_id].consulted_by,
            "in_effect": in_effect(cfg, risk_id),
        }
        for risk_id in sorted(effective(cfg))
    ]
    if module_risks is not None:
        rows += [
            {
                "risk": risk_id,
                "weakens": item.weakens,
                "consulted_by": item.consulted_by,
                "in_effect": _is_prod(cfg) or item.outside_prod,
            }
            for risk_id, item in module_risks.active
        ]
        rows += [
            {
                "risk": risk_id,
                "applicable": False,
                "note": "not applicable here: its module is not loaded in this process",
            }
            for risk_id in module_risks.inert
        ]
    return rows


__all__ = [
    "MODULE_RISK_ID",
    "RISKS",
    "RISK_IDS",
    "ModuleRiskReport",
    "Risk",
    "accepts",
    "classify_module_risks",
    "diagnostics",
    "effective",
    "in_effect",
    "is_module_id",
    "replacement_ids",
    "risk",
    "unknown_ids",
    "unknown_ids_message",
]
