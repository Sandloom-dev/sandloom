"""Logging setup: redaction, request/trace correlation, optional JSON output.

Every log record is enriched with the current ``request_id`` and ``trace_id``
(from the request-id middleware and the tracer) so logs correlate to a request
and a distributed trace. In production (or when ``LOG_FORMAT=json``) records are
emitted as single-line JSON for ingestion by log pipelines; otherwise a compact
human-readable line is used. Sensitive values are redacted in both modes.
"""

import json
import logging
from typing import Any

import sandloom.core.observability.request_context as _request_context
import sandloom.core.observability.tracing as _tracing
from sandloom.core.config.settings import settings

#: Bound by assignment (keeps the ``.get()`` method-call fast path).
_request_id_var = _request_context.request_id_var

try:  # local import; guardrails optional at early bootstrap
    from sandloom.core.observability.guardrails import (
        is_sensitive_key,
        redact,
        scrub_text,
    )
except Exception:  # pragma: no cover  # noqa: BLE001 - redaction falls back to a no-op

    def redact(obj: Any) -> Any:  # fallback no-op
        return obj

    def scrub_text(text: str) -> str:  # fallback no-op
        return text

    def is_sensitive_key(key: str) -> bool:  # fallback: nothing sensitive
        return False


# Standard LogRecord attributes — everything else on a record is an "extra".
# (Defined before _redact_record uses it.)
_RESERVED_RECORD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__.keys()) | {
    "request_id",
    "trace_id",
    "message",
    "asctime",
}


class ContextFilter(logging.Filter):
    """Attach request_id and trace_id to every record (None when absent)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _current_request_id()
        record.trace_id = _current_trace_id()
        return True


class AccessLogFilter(ContextFilter):
    """Uvicorn access logs omit query values before any formatter sees them."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) == 5:
            values = list(record.args)
            if isinstance(values[2], str):
                values[2] = values[2].partition("?")[0]
                record.args = tuple(values)
        return super().filter(record)


def _current_request_id() -> str | None:
    return _request_id_var.get()


def _current_trace_id() -> str | None:
    try:
        return _tracing.get_current_trace_id()
    except Exception:  # pragma: no cover  # noqa: BLE001 - never break logging
        return None


def _redact_record(record: logging.LogRecord) -> None:  # noqa: C901
    """Apply value redaction to dict messages, args, and sensitive attrs."""
    if isinstance(record.msg, dict):
        record.msg = redact(record.msg)
    if record.args:
        if isinstance(record.args, tuple):
            new_args: list[Any] = []
            changed = False
            for a in record.args:
                if isinstance(a, dict):
                    new_args.append(redact(a))
                    changed = True
                else:
                    new_args.append(a)
            if changed:
                record.args = tuple(new_args)
        elif isinstance(record.args, dict):  # mapping style formatting
            record.args = redact(record.args)
    # Extras (logger.x(..., extra={...})) are matched by SUBSTRING like every
    # other redaction path, so ``refresh_token`` / ``db_password`` are caught,
    # not just an attribute literally named ``token``.
    for attr in list(record.__dict__.keys()):
        if attr in _RESERVED_RECORD_ATTRS or attr.startswith("_"):
            continue
        value = record.__dict__[attr]
        if is_sensitive_key(attr):
            setattr(record, attr, "***")
        elif isinstance(value, dict | list | tuple):
            setattr(record, attr, redact(value))
        elif isinstance(value, str):
            setattr(record, attr, scrub_text(value))


class RedactingFormatter(logging.Formatter):  # pragma: no cover
    """Human-readable formatter that redacts sensitive values first."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            _redact_record(record)
        except Exception:  # noqa: BLE001, S110 - redaction must never break logging
            pass
        # Scrub the fully rendered line (message incl. f-string content AND
        # the traceback) for embedded DSN credentials / SQL params / tokens.
        base = scrub_text(super().format(record))
        rid = getattr(record, "request_id", None)
        if rid:
            base = f"{base} [request_id={rid}]"
        return base


class JsonFormatter(logging.Formatter):  # pragma: no cover
    """Structured single-line JSON formatter for log pipelines."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            _redact_record(record)
        except Exception:  # noqa: BLE001, S110 - redaction must never break logging
            pass
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": scrub_text(record.getMessage()),
        }
        rid = getattr(record, "request_id", None)
        if rid:
            payload["request_id"] = rid
        tid = getattr(record, "trace_id", None)
        if tid:
            payload["trace_id"] = tid
        if record.exc_info:
            payload["exc_info"] = scrub_text(self.formatException(record.exc_info))
        # Include any structured extras passed via logger(..., extra={...}).
        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_ATTRS and not key.startswith("_"):
                payload[key] = _json_safe(value)
        return json.dumps(payload, default=str, ensure_ascii=False)


def _json_safe(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, int | float | bool) or value is None:
        return value
    if isinstance(value, dict):
        return redact(value)
    return str(value)


def init_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(level=level)

    formatter: logging.Formatter
    if settings.log_as_json:
        formatter = JsonFormatter()
    else:
        formatter = RedactingFormatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )
    context_filter = ContextFilter()
    root = logging.getLogger()
    root.setLevel(level)
    for handler in root.handlers:
        try:
            # Latest settings win for the formatter; the filter is attached
            # once (create_app calls this per app build: tests, --reload).
            handler.setFormatter(formatter)
            if not any(type(f) is ContextFilter for f in handler.filters):
                handler.addFilter(context_filter)
        except Exception:  # pragma: no cover  # noqa: BLE001, S110 - never break logging setup
            pass
