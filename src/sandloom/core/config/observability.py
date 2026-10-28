"""Error reporting, tracing and logging."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ObservabilityConfig(BaseModel):
    """Error reporting, tracing and logging."""

    # Observability
    sentry_dsn: str = Field(default="", alias="SENTRY_DSN")
    sentry_traces_sample_rate: float = Field(
        default=0.0, alias="SENTRY_TRACES_SAMPLE_RATE"
    )
    # OTLP endpoint; tracing is enabled only when set (and the SDK installed).
    tracing_url: str = Field(default="", alias="TRACING_URL")
    otel_exporter_otlp_protocol: Literal["grpc", "http/protobuf"] = Field(
        default="grpc", alias="OTEL_EXPORTER_OTLP_PROTOCOL"
    )
    otel_service_name: str = Field(
        default="sandloom-backend", alias="OTEL_SERVICE_NAME"
    )
    # Logging: level + format. format=auto => JSON in prod, human text otherwise.
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_format: Literal["auto", "json", "text"] = Field(
        default="auto", alias="LOG_FORMAT"
    )
