"""Error reporting, tracing and logging."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ObservabilityConfig(BaseModel):
    """Error reporting, tracing and logging."""

    # Observability
    sentry_dsn: str = Field(
        default="",
        alias="SENTRY_DSN",
        description=("Sentry project DSN; empty disables Sentry reporting."),
    )
    sentry_traces_sample_rate: float = Field(
        default=0.0,
        alias="SENTRY_TRACES_SAMPLE_RATE",
        description=(
            "Fraction of transactions sampled for Sentry tracing when Sentry is "
            "configured."
        ),
    )
    # OTLP endpoint; tracing is enabled only when set (and the SDK installed).
    tracing_url: str = Field(
        default="",
        alias="TRACING_URL",
        description=(
            "OpenTelemetry collector endpoint; empty disables OTLP export, which "
            "also requires the tracing SDK."
        ),
    )
    otel_exporter_otlp_protocol: Literal["grpc", "http/protobuf"] = Field(
        default="grpc",
        alias="OTEL_EXPORTER_OTLP_PROTOCOL",
        description=(
            "OTLP exporter protocol used with TRACING_URL: grpc or http/protobuf."
        ),
    )
    otel_service_name: str = Field(
        default="sandloom-backend",
        alias="OTEL_SERVICE_NAME",
        description=("Service name attached to exported OpenTelemetry telemetry."),
    )
    # Logging: level + format. format=auto => JSON in prod, human text otherwise.
    log_level: str = Field(
        default="INFO",
        alias="LOG_LEVEL",
        description=(
            "Minimum process log level, using standard Python logging levels."
        ),
    )
    log_format: Literal["auto", "json", "text"] = Field(
        default="auto",
        alias="LOG_FORMAT",
        description=(
            "Log output format: auto selects JSON in production and "
            "human-readable text elsewhere."
        ),
    )
