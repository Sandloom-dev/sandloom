"""Error reporting (Sentry) initialization.

Best-effort and fully optional: if ``SENTRY_DSN`` is unset or the ``sentry-sdk``
package is not installed, this is a no-op. When configured, unhandled
exceptions and ERROR logs are reported with the environment tag set.
"""

from __future__ import annotations

import logging
from typing import Any

from sandloom.core.config.settings import settings
from sandloom.core.observability.guardrails import redact, scrub_text

logger = logging.getLogger("sandloom.core.observability.observability")

_sentry_initialized = False


def scrub_sentry_event(event: Any, hint: Any = None) -> Any:
    """``before_send`` hook: strip secrets before an event leaves the process.

    * request headers / cookies / data / env, ``extra``, ``contexts`` and
      breadcrumbs are key-redacted (``Authorization``, ``password``, ...);
    * exception values, log messages and breadcrumb messages are free-text
      scrubbed (DSN credentials, SQL bound parameters, bearer tokens);
    * stack-frame local variables are dropped entirely (they routinely hold the
      request body, the session, decrypted secrets).
    Never raises: on any internal error the event is dropped rather than sent
    unscrubbed.
    """
    try:
        if not isinstance(event, dict):
            return event
        for key in ("request", "extra", "contexts", "tags", "user"):
            if key in event and isinstance(event[key], dict):
                event[key] = redact(event[key])
        _scrub_request_text(event)
        _scrub_logentry(event)
        if isinstance(event.get("message"), str):
            event["message"] = scrub_text(event["message"])
        _scrub_exceptions(event)
        _scrub_breadcrumbs(event)
        return event
    except Exception:  # noqa: BLE001 - fail closed: drop rather than leak
        return None


# The helpers below may raise: scrub_sentry_event then drops the event.


def _scrub_request_text(event: dict[str, Any]) -> None:
    request = event.get("request")
    if isinstance(request, dict):
        for field in ("cookies", "data", "query_string"):
            if field in request and isinstance(request[field], str):
                request[field] = scrub_text(request[field])


def _scrub_logentry(event: dict[str, Any]) -> None:
    logentry = event.get("logentry")
    if isinstance(logentry, dict):
        if isinstance(logentry.get("message"), str):
            logentry["message"] = scrub_text(logentry["message"])
        if isinstance(logentry.get("formatted"), str):
            logentry["formatted"] = scrub_text(logentry["formatted"])
        logentry.pop("params", None)


def _scrub_exceptions(event: dict[str, Any]) -> None:
    """Scrub exception values; drop every stack frame's local variables."""
    exc = event.get("exception")
    values = exc.get("values") if isinstance(exc, dict) else None
    for value in values or []:
        if isinstance(value, dict):
            if isinstance(value.get("value"), str):
                value["value"] = scrub_text(value["value"])
            stack = value.get("stacktrace")
            frames = stack.get("frames") if isinstance(stack, dict) else None
            for frame in frames or []:
                if isinstance(frame, dict):
                    frame.pop("vars", None)


def _scrub_breadcrumbs(event: dict[str, Any]) -> None:
    crumbs = event.get("breadcrumbs")
    crumb_values = crumbs.get("values") if isinstance(crumbs, dict) else crumbs
    for crumb in crumb_values or []:
        if isinstance(crumb, dict):
            if isinstance(crumb.get("message"), str):
                crumb["message"] = scrub_text(crumb["message"])
            if isinstance(crumb.get("data"), dict):
                crumb["data"] = redact(crumb["data"])


def init_sentry() -> bool:
    """Initialize Sentry if configured. Returns True when enabled."""
    global _sentry_initialized
    if _sentry_initialized:
        return True

    dsn = settings.sentry_dsn.strip()
    if not dsn:
        return False
    try:
        import sentry_sdk  # noqa: PLC0415 - optional: optional dependency
        from sentry_sdk.integrations.logging import LoggingIntegration  # noqa: PLC0415 - optional: optional dependency, imported only when present

        sentry_sdk.init(
            dsn=dsn,
            environment=settings.environment.value,
            traces_sample_rate=settings.sentry_traces_sample_rate,
            # Never ship PII / frame locals by default; scrub what remains.
            send_default_pii=False,
            include_local_variables=False,
            before_send=scrub_sentry_event,
            before_send_transaction=scrub_sentry_event,
            integrations=[
                LoggingIntegration(level=logging.INFO, event_level=logging.ERROR)
            ],
        )
        _sentry_initialized = True
        logger.info("Sentry error reporting enabled")
        return True
    except Exception:  # never let observability break startup
        logger.warning("Sentry DSN set but initialization failed", exc_info=True)
        return False


__all__ = ["init_sentry", "scrub_sentry_event"]
