"""Saga runtime (ADR-0008): timer sweep, retry backoff, compensation window,
retention. Every field is an ops knob (capacity / operations): none changes a
guarantee — the window between them and the command dedupe TTL is checked by
``sandloom.core.guarantee_windows`` (``saga_horizon_beyond_command_dedupe``).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SagasConfig(BaseModel):
    """Saga runtime knobs (``sandloom.core.sagas``)."""

    # The lease-guarded timer sweep (one holder per database) re-drives due
    # instances (backoff elapsed, deadline passed) at most once per interval.
    saga_sweep_interval_seconds: float = Field(
        default=5.0, gt=0, le=3600, alias="SAGA_SWEEP_INTERVAL_SECONDS"
    )
    # Instances one sweep tick nudges per database (FOR UPDATE SKIP LOCKED).
    saga_sweep_batch: int = Field(
        default=200, ge=1, le=10_000, alias="SAGA_SWEEP_BATCH"
    )
    # Default window for a compensation to finish (transient failures retried
    # with capped backoff until then, then parked); a definition overrides it.
    saga_compensation_deadline_seconds: int = Field(
        default=3600, ge=1, alias="SAGA_COMPENSATION_DEADLINE_SECONDS"
    )
    # Exponential backoff with full jitter for transient step failures:
    # uniform(0, min(max, base * 2**(attempt-1))).
    saga_retry_base_seconds: float = Field(
        default=1.0, gt=0, alias="SAGA_RETRY_BASE_SECONDS"
    )
    saga_retry_max_seconds: float = Field(
        default=300.0, gt=0, alias="SAGA_RETRY_MAX_SECONDS"
    )
    # Terminal instances (completed / compensated) older than this are purged
    # by the sweep holder (hourly); parked instances are never purged.
    # 0 keeps them forever.
    saga_retention_days: int = Field(default=30, ge=0, alias="SAGA_RETENTION_DAYS")
