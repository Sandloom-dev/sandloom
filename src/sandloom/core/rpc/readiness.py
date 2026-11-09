"""Readiness / diagnostics probes of remote bindings: reachability and the
rolling-upgrade contract compatibility check.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from fastapi import status

from sandloom.core import contract_compat
from sandloom.core.config.settings import settings as _global_settings
from sandloom.core.descriptors import HealthResult
from sandloom.core.rpc.binding import _bound_get
from sandloom.core.rpc.client import _httpx, _rpc_client
from sandloom.core.rpc.errors import RemoteServiceUnavailable, _RpcRefused
from sandloom.core.rpc.outbound import _get_auth, _outbound_mac
from sandloom.core.rpc.wire import ACCESS_LIFETIME_DESCRIBE_KEY, _unpack

logger = logging.getLogger("sandloom.core.rpc")


def _binding_is_required(binding: dict[str, object]) -> bool:
    """Whether an unreachable peer must fail the caller's readiness.

    Opt-in (``"required": true``); the default is ``False``. A peer outage
    degrades only the calls that need it (``RemoteServiceUnavailable`` → 503
    at call time) — it never fails liveness (``/health``) and, unless opted in,
    never readiness: otherwise one stopped service drains every consumer from
    the load balancer (and, with mutual bindings, deadlocks a cold start).
    """
    return binding.get("required") is True


@dataclass(frozen=True)
class _ProbeOutcome:
    """One status probe: ``failure`` (``None`` = serves the contract), the
    provider's describe document when it answered 200, and whether a 404 was
    the provider's definite ``service_not_found`` (not a hidden-endpoint 404)."""

    failure: dict[str, object] | None
    contract: dict[str, Any] | None = None
    not_served: bool = False


async def _probe_binding(
    service_name: str,
    base_url: str,
    timeout_seconds: float,
    expect_service: str | None = None,
    cfg: Any = None,
) -> _ProbeOutcome:
    """One status probe (``GET /_sandloom/rpc/<contract>`` = the describe document)."""
    cfg = cfg if cfg is not None else _global_settings
    try:
        mac_key = _outbound_mac(cfg, expect_service, service_name)
        headers, signer = _get_auth(service_name, cfg, mac_key, expect_service)
        response = await _bound_get(
            _rpc_client(),
            f"{base_url}/_sandloom/rpc/{quote(service_name, safe='.')}",
            headers,
            timeout_seconds,
            base_url.rstrip("/"),
            expect_service,
            cfg,
            signer=signer,
        )
    except RemoteServiceUnavailable as exc:
        return _ProbeOutcome(
            {"base_url": base_url, "error": str(exc.detail), "code": exc.code}
        )
    except _RpcRefused as exc:
        return _ProbeOutcome(
            {"base_url": base_url, "status_code": exc.status_code, "code": exc.code}
        )
    except _httpx().HTTPError as exc:
        return _ProbeOutcome({"base_url": base_url, "error": exc.__class__.__name__})
    if response.status_code != status.HTTP_200_OK:
        not_served = False
        if response.status_code == status.HTTP_404_NOT_FOUND:
            with suppress(Exception):
                body = _unpack(response.content)
                error = body.get("error") if isinstance(body, dict) else None
                not_served = (
                    isinstance(error, dict) and error.get("code") == "service_not_found"
                )
        return _ProbeOutcome(
            {
                "base_url": base_url,
                "status_code": response.status_code,
                "error": response.text[:300],
            },
            not_served=not_served,
        )
    contract: dict[str, Any] | None = None
    with suppress(Exception):
        data = _unpack(response.content)
        described = data.get("contract") if isinstance(data, dict) else None
        if isinstance(described, dict) and isinstance(described.get("methods"), dict):
            contract = described
    return _ProbeOutcome(None, contract=contract)


def _compat_mode(core: Any) -> str:
    mode = getattr(getattr(core, "settings", None), "contract_compat_check", "off")
    return mode if mode in ("ready", "strict") else "off"


#: (base_url, timeout_seconds, pinned provider) of one probe.
_ProbeTarget = tuple[str, float, str | None]


async def _probe_remote(
    core: Any, *, reachability: set[str] | None, compat: bool
) -> HealthResult:
    """Probe remote bindings for reachability and/or contract compatibility.

    ``reachability``: contracts whose unreachable peer is a failure (``None``
    = every remote binding). ``compat``: also compare each binding with what
    this caller expects of it (:func:`contract_compat.resolve_expectations`,
    resolved once at startup) against the provider's live describe document:
    an incompatible provider is a failure, and so is a required contract
    with no expectation (``contract_unverified``, ADR-0013 decision 4); a
    peer that cannot be reached is NOT (reachability is the other check's job).
    """

    cfg = core.settings
    remote = {
        name: binding
        for name, binding in cfg.service_bindings.items()
        if not core.service_registry.contains(name)
    }
    plan = contract_compat.resolve_expectations(core, remote) if compat else None
    expectations = plan.expectations if plan is not None else {}
    failures: dict[str, object] = {}
    unverified: dict[str, object] = {}
    probes = _probe_targets(cfg, remote, reachability, expectations, failures)

    names = sorted(probes)
    outcomes = await asyncio.gather(
        *(_probe_binding(name, *probes[name], cfg=cfg) for name in names)
    )
    for name, outcome in zip(names, outcomes, strict=True):
        reach = reachability is None or name in reachability
        expected = expectations.get(name, ())
        if outcome.failure is not None:
            failure = _probe_failure(name, outcome, reach, expected, probes[name][0])
            if failure is not None:
                failures[name] = failure
            continue
        drift = _token_lifetime_drift(cfg, outcome.contract)
        if drift is not None:
            failures[name] = {"config_drift": [drift]}
            continue
        if not expected:
            continue
        if outcome.contract is None:
            unverified[name] = ["the provider published no contract description"]
            continue
        _compare(name, expected, outcome.contract, failures, unverified)
    for name, reasons in (plan.unverified if plan is not None else {}).items():
        entry = failures.get(name)
        failures[name] = {
            **(entry if isinstance(entry, dict) else {}),
            "contract_unverified": list(reasons),
        }
    return _probe_result(names, failures, unverified)


def _compare(
    name: str,
    expected: tuple[contract_compat.Expectation, ...],
    actual: dict[str, Any],
    failures: dict[str, object],
    unverified: dict[str, object],
) -> None:
    """Hold the provider's describe document to every consumer's expectation."""
    errors: list[str] = []
    warnings: list[str] = []
    sources: list[str] = []
    for expectation in expected:
        report = contract_compat.compare_contract(
            name, expectation.describe, actual, methods=expectation.methods
        )
        errors += [e for e in report.errors if e not in errors]
        warnings += [w for w in report.warnings if w not in warnings]
        if report.errors:
            sources.append(expectation.source)
    if errors:
        failures[name] = {
            "contract_incompatible": errors,
            "expected_from": sources[0] if len(sources) == 1 else sources,
        }
    if warnings:
        unverified[name] = warnings


def _token_lifetime_drift(cfg: Any, contract: dict[str, Any] | None) -> str | None:
    """A reason when the provider's access-token lifetime differs from ours.

    ``ACCESS_TOKEN_EXPIRE_MINUTES`` must be identical in every service: the
    revocation-mark TTL is derived from it, so a provider with a longer
    lifetime could accept a revoked token after its mark expired. A provider
    that does not publish it (an older release) is not judged.
    """
    theirs = (contract or {}).get(ACCESS_LIFETIME_DESCRIBE_KEY)
    ours = getattr(cfg, "access_token_expire_minutes", None)
    if not isinstance(theirs, int) or not isinstance(ours, int) or theirs == ours:
        return None
    return (
        f"access_token_expire_minutes differs: provider {theirs}, this service "
        f"{ours} (ACCESS_TOKEN_EXPIRE_MINUTES must be identical in every service)"
    )


def _probe_targets(
    cfg: Any,
    remote: dict[str, Any],
    reachability: set[str] | None,
    expectations: Mapping[str, Any],
    failures: dict[str, object],
) -> dict[str, _ProbeTarget]:
    """What to probe per remote binding (a binding without a URL that must be
    reachable is recorded in ``failures`` instead)."""
    probe_cap = float(getattr(cfg, "service_probe_timeout_seconds", 1.0))
    default_timeout = float(cfg.service_rpc_timeout_seconds)
    probes: dict[str, _ProbeTarget] = {}
    for service_name, binding in remote.items():
        reach = reachability is None or service_name in reachability
        if not reach and service_name not in expectations:
            continue
        base_url = str(binding.get("base_url") or binding.get("url") or "").rstrip("/")
        if not base_url:
            if reach:
                failures[service_name] = {"error": "missing URL"}
            continue
        timeout = binding.get("timeout_seconds")
        timeout_seconds = min(
            float(timeout)
            if isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
            else default_timeout,
            probe_cap,
        )
        pinned = binding.get("service_id")
        probes[service_name] = (
            base_url,
            timeout_seconds,
            str(pinned) if pinned else None,
        )
    return probes


def _probe_failure(
    name: str,
    outcome: _ProbeOutcome,
    reach: bool,
    expected: tuple[contract_compat.Expectation, ...],
    base_url: str,
) -> dict[str, object] | None:
    """The failure a failed probe records (``None``: it does not count)."""
    if expected and outcome.not_served:
        return {
            "contract_incompatible": [
                f"{name}: not served by the provider at {base_url}"
            ],
            "expected_from": expected[0].source,
        }
    return outcome.failure if reach else None


def _probe_result(
    names: list[str], failures: dict[str, object], unverified: dict[str, object]
) -> HealthResult:
    details: dict[str, object] = {}
    if unverified:
        details["contracts_unverified"] = unverified
    if failures:
        details["remote_services"] = failures
        return HealthResult(status="fail", details=details)
    details["probed"] = names
    return HealthResult(status="ok", details=details)


async def check_remote_service_bindings(
    core: Any, *, only_required: bool = True
) -> HealthResult:
    """Probe remote service bindings that are not provided locally.

    Readiness probes only bindings marked ``"required": true`` (see
    :func:`_binding_is_required`) for reachability; ``only_required=False``
    probes every remote binding (``/diagnostics/services``). Probes run
    CONCURRENTLY over the pooled keep-alive client (bindings sharing a
    base_url share connections) and each is capped at
    ``SERVICE_PROBE_TIMEOUT_SECONDS`` — so the whole check costs about one
    probe timeout however many bindings point at a down peer, instead of one
    timeout per binding.

    With ``CONTRACT_COMPAT_CHECK`` on (default ``ready``) every binding whose
    expected contract snapshot is known is ALSO compared with the provider's
    live describe document; an incompatible provider (removed method, newly
    required parameter, incompatible type, ...) fails the check with a
    precise ``contract_incompatible`` diagnostic, whether or not the binding
    is ``required`` — an unreachable non-required peer still never does.
    """
    cfg = core.settings
    reachability = (
        {
            name
            for name, binding in cfg.service_bindings.items()
            if _binding_is_required(binding)
        }
        if only_required
        else None
    )
    return await _probe_remote(
        core, reachability=reachability, compat=_compat_mode(core) != "off"
    )


class ContractIncompatibleError(RuntimeError):
    """A reachable remote provider is incompatible with this caller's contract
    snapshot (``CONTRACT_COMPAT_CHECK=strict`` aborts startup with it)."""


class ContractUnverifiedError(ContractIncompatibleError):
    """A required remote contract has no expectation to check it against
    (``CONTRACT_COMPAT_CHECK=strict`` aborts startup with it before any probe;
    ADR-0013 decision 4)."""


async def check_remote_contract_compat(core: Any) -> HealthResult:
    """Compare every remote binding with its expectation (no reachability).

    The startup half of the rolling-upgrade guard. It resolves what this
    caller expects of each remote contract (read once here; ``/ready`` reuses
    it). A required contract with no expectation is refused before any probe
    under ``strict`` (:class:`ContractUnverifiedError`) and logged at ERROR
    under ``ready``, whose ``/ready`` then fails with ``contract_unverified``.
    An incompatible provider is logged (``ready``) or raises
    :class:`ContractIncompatibleError` (``strict``). An unreachable peer is
    only logged: it may simply start later; readiness keeps re-checking.
    """
    mode = _compat_mode(core)
    if mode == "off":
        return HealthResult(status="ok", details={"contract_compat": "off"})

    remote = sorted(
        name
        for name in core.settings.service_bindings
        if not core.service_registry.contains(name)
    )
    plan = contract_compat.resolve_expectations(core, remote)
    for name, reasons in sorted(plan.unverified.items()):
        logger.error("Remote contract %s cannot be checked: %s", name, reasons)
    if plan.unverified and mode == "strict":
        raise ContractUnverifiedError(
            "remote contract(s) cannot be checked (no expectation): "
            + "; ".join(r for reasons in plan.unverified.values() for r in reasons)
        )
    result = await _probe_remote(core, reachability=set(), compat=True)
    details = result.details or {}
    unverified = details.get("contracts_unverified")
    for name, warnings_ in (unverified if isinstance(unverified, dict) else {}).items():
        logger.info("Contract %s compatibility unverified: %s", name, warnings_)
    if result.status != "ok":
        problems = details.get("remote_services")
        logger.error("Remote contract(s) incompatible with this release: %s", problems)
        if mode == "strict":
            raise ContractIncompatibleError(
                f"remote contract(s) incompatible with this release: {problems}"
            )
    return result
