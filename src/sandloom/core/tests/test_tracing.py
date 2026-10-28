"""Tracing helpers: span semantics and settings-driven enablement (R5)."""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from sandloom.core.config.settings import settings
from sandloom.core.observability import tracing


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []


@pytest.fixture
def fake_otel(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """Install a minimal fake ``opentelemetry`` SDK in ``sys.modules``."""
    rec = _Recorder()

    @contextmanager
    def start_as_current_span(name: str) -> Iterator[object]:
        rec.calls.append(("span", name))
        yield object()  # like the SDK: records, then re-raises a body error

    tracer = types.SimpleNamespace(start_as_current_span=start_as_current_span)
    trace_mod = types.ModuleType("opentelemetry.trace")
    trace_mod.get_tracer = lambda _name: tracer  # type: ignore[attr-defined]
    trace_mod.set_tracer_provider = lambda p: rec.calls.append(  # type: ignore[attr-defined]
        ("provider", p)
    )

    class OTLPSpanExporter:
        def __init__(self, *, endpoint: str, insecure: bool) -> None:
            rec.calls.append(("exporter", (endpoint, insecure)))

    class HTTPSpanExporter:
        def __init__(self, *, endpoint: str) -> None:
            rec.calls.append(("http_exporter", endpoint))

    class Resource:
        @staticmethod
        def create(attrs: dict[str, str]) -> dict[str, str]:
            rec.calls.append(("resource", attrs))
            return attrs

    class TracerProvider:
        def __init__(self, *, resource: Any) -> None:
            self.resource = resource

        def add_span_processor(self, _p: Any) -> None:
            pass

        def shutdown(self) -> None:
            rec.calls.append(("shutdown", None))

    class BatchSpanProcessor:
        def __init__(self, _exporter: Any) -> None:
            pass

    root = types.ModuleType("opentelemetry")
    root.trace = trace_mod  # type: ignore[attr-defined]
    exporter = types.ModuleType("opentelemetry.exporter.otlp.proto.grpc.trace_exporter")
    exporter.OTLPSpanExporter = OTLPSpanExporter  # type: ignore[attr-defined]
    http_exporter = types.ModuleType(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter"
    )
    http_exporter.OTLPSpanExporter = HTTPSpanExporter  # type: ignore[attr-defined]
    resources = types.ModuleType("opentelemetry.sdk.resources")
    resources.Resource = Resource  # type: ignore[attr-defined]
    sdk_trace = types.ModuleType("opentelemetry.sdk.trace")
    sdk_trace.TracerProvider = TracerProvider  # type: ignore[attr-defined]
    export = types.ModuleType("opentelemetry.sdk.trace.export")
    export.BatchSpanProcessor = BatchSpanProcessor  # type: ignore[attr-defined]
    for name, mod in {
        "opentelemetry": root,
        "opentelemetry.trace": trace_mod,
        "opentelemetry.exporter": types.ModuleType("opentelemetry.exporter"),
        "opentelemetry.exporter.otlp": types.ModuleType("opentelemetry.exporter.otlp"),
        "opentelemetry.exporter.otlp.proto": types.ModuleType(
            "opentelemetry.exporter.otlp.proto"
        ),
        "opentelemetry.exporter.otlp.proto.grpc": types.ModuleType(
            "opentelemetry.exporter.otlp.proto.grpc"
        ),
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": exporter,
        "opentelemetry.exporter.otlp.proto.http": types.ModuleType(
            "opentelemetry.exporter.otlp.proto.http"
        ),
        "opentelemetry.exporter.otlp.proto.http.trace_exporter": http_exporter,
        "opentelemetry.sdk": types.ModuleType("opentelemetry.sdk"),
        "opentelemetry.sdk.resources": resources,
        "opentelemetry.sdk.trace": sdk_trace,
        "opentelemetry.sdk.trace.export": export,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setattr(tracing, "_tracing_initialized", False)
    monkeypatch.setattr(tracing, "_tracing_enabled", False)
    monkeypatch.setattr(tracing, "_provider", None)
    monkeypatch.setattr(settings, "otel_exporter_otlp_protocol", "grpc")
    return rec


def test_body_exception_propagates_unchanged_through_span(
    fake_otel: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body error (e.g. an HTTPException 404) must not become a
    ``RuntimeError: generator didn't stop after throw()`` (a 500)."""
    monkeypatch.setattr(tracing, "_tracing_enabled", True)
    with pytest.raises(LookupError, match="not found"):
        with tracing.start_span("op"):
            raise LookupError("not found")
    assert fake_otel.calls == [("span", "op")]


def test_span_setup_failure_degrades_to_a_noop_span(
    fake_otel: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tracing, "_tracing_enabled", True)

    def broken(_name: str) -> Any:
        raise RuntimeError("sdk broken")

    monkeypatch.setattr(sys.modules["opentelemetry.trace"], "get_tracer", broken)
    with tracing.start_span("op") as span:
        assert span is None


def test_tracing_stays_off_without_tracing_url(
    fake_otel: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "tracing_url", "")
    tracing.init_tracing()
    assert tracing._tracing_enabled is False
    assert fake_otel.calls == []


def test_tracing_is_configured_from_settings(
    fake_otel: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "tracing_url", "https://collector:4317")
    monkeypatch.setattr(settings, "otel_service_name", "billing-svc")
    tracing.init_tracing()
    assert tracing._tracing_enabled is True
    assert ("resource", {"service.name": "billing-svc"}) in fake_otel.calls
    assert ("exporter", ("https://collector:4317", False)) in fake_otel.calls


def test_otlp_http_uses_http_exporter_and_flushes_once(
    fake_otel: _Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = "https://collector:4318/v1/traces"
    monkeypatch.setattr(settings, "tracing_url", endpoint)
    monkeypatch.setattr(settings, "otel_exporter_otlp_protocol", "http/protobuf")
    tracing.init_tracing()
    assert ("http_exporter", endpoint) in fake_otel.calls
    assert not any(name == "exporter" for name, _ in fake_otel.calls)
    tracing.shutdown_tracing()
    tracing.shutdown_tracing()
    assert fake_otel.calls.count(("shutdown", None)) == 1
