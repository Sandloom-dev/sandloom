"""Tracing integration (Phase H initial implementation).

This module performs a lazy, best-effort OpenTelemetry setup when
``TRACING_URL`` is set and the OpenTelemetry SDK/exporter are installed.
Configuration failures are logged and do not interrupt application startup.

Settings (``sandloom.core.config.settings``):
  TRACING_URL — OTLP endpoint; tracing is off while it is empty
  OTEL_SERVICE_NAME — the ``service.name`` resource (default 'sandloom-backend')
  OTEL_EXPORTER_OTLP_PROTOCOL — grpc (default) or http/protobuf

Provided helpers:
  - init_tracing(): idempotent initialization
  - start_span(name): context manager for ad-hoc spans (no-op if tracing off)
  - shutdown_tracing(): flush and close this integration's provider
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any

from sandloom.core.config.settings import settings

_tracing_initialized = False
_tracing_enabled = False
_provider: Any = None


def init_tracing() -> None:
    global _tracing_initialized, _tracing_enabled, _provider
    if _tracing_initialized:
        return
    _tracing_initialized = True

    endpoint = settings.tracing_url
    if not endpoint:
        _tracing_enabled = False
        return
    try:
        # Optional imports
        from opentelemetry import trace  # noqa: PLC0415 - optional: optional dependency, imported only when present
        from opentelemetry.sdk.resources import Resource  # noqa: PLC0415 - optional: optional dependency, imported only when present
        from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415 - optional: optional dependency, imported only when present
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415 - optional: optional dependency, imported only when present

        resource = Resource.create({"service.name": settings.otel_service_name})
        provider = TracerProvider(resource=resource)
        protocol = settings.otel_exporter_otlp_protocol
        if protocol == "http/protobuf":
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415 - optional: optional dependency, imported only when present
                OTLPSpanExporter as HTTPSpanExporter,
            )

            span_exporter: Any = HTTPSpanExporter(endpoint=endpoint)
        elif protocol == "grpc":
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415 - optional: optional dependency, imported only when present
                OTLPSpanExporter,
            )

            # TLS for https/grpcs endpoints; plaintext only for local/http.
            insecure = not endpoint.startswith(("https://", "grpcs://"))
            span_exporter = OTLPSpanExporter(endpoint=endpoint, insecure=insecure)
        else:
            raise ValueError(
                "OTEL_EXPORTER_OTLP_PROTOCOL must be grpc or http/protobuf"
            )
        provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(provider)
        _provider = provider
        _tracing_enabled = True
    except Exception:
        logging.getLogger(__name__).warning(
            "Tracing initialization failed; tracing is disabled", exc_info=True
        )
        _tracing_enabled = False


def shutdown_tracing() -> None:
    """Flush and close only the provider created by this integration, once."""
    global _provider, _tracing_enabled
    provider, _provider = _provider, None
    _tracing_enabled = False
    if provider is not None:
        try:
            provider.shutdown()
        except Exception:
            logging.getLogger(__name__).warning(
                "Tracing shutdown failed", exc_info=True
            )


@contextmanager
def start_span(name: str) -> Iterator[object]:
    """A span around the body; a no-op (``None``) when tracing is off.

    Only span SETUP failures degrade to a no-op: an exception raised by the
    body propagates unchanged (yielding again after it would turn it into
    ``RuntimeError: generator didn't stop after throw()``).
    """
    if not _tracing_enabled:
        yield None
        return
    try:
        from opentelemetry import trace  # noqa: PLC0415 - optional: optional dependency, imported only when present

        stack = ExitStack()
        span = stack.enter_context(
            trace.get_tracer("sandloom.core").start_as_current_span(name)
        )
    except Exception:  # noqa: BLE001 - tracing must never break the caller
        yield None
        return
    with stack:
        yield span


def get_current_trace_id() -> str | None:  # pragma: no cover - lightweight
    """Return current active trace id (hex) if tracing enabled, else None."""
    if not _tracing_enabled:
        return None
    try:
        from opentelemetry import trace  # noqa: PLC0415 - optional: optional dependency, imported only when present

        span = trace.get_current_span()
        ctx = span.get_span_context()
        trace_val = getattr(ctx, "trace_id", 0)
        try:
            if isinstance(trace_val, int) and trace_val != 0:
                return f"{trace_val:032x}"
        except Exception:  # pragma: no cover  # noqa: BLE001 - tracing must never break the caller
            return None
    except Exception:  # noqa: BLE001 - tracing must never break the caller
        return None
    return None
