"""Every dedupe / replay record uses the shared bounds (review hand-off).

The 24 h record TTL and the 256 KiB replay cap were duplicated literals in
the HTTP idempotency guard, the command dedupe, the consumer inbox, the task-queue enqueue dedupe and the settings default; they
now all derive from :mod:`sandloom.core.config.replay_limits`.
"""

from __future__ import annotations

import inspect

import sandloom.core.config.replay_limits as replay_limits
from sandloom.core import di
from sandloom.core.config.settings import Settings
from sandloom.core.drivers.celery_taskqueue import CeleryTaskQueueOptions
from sandloom.core.eventing.consumer_inbox import ConsumerInbox
from sandloom.core.idempotency import capture as idempotency_capture
from sandloom.core.idempotency import fingerprint as idempotency_fingerprint
from sandloom.core.module_api import _dedupe

TTL = replay_limits.REPLAY_RECORD_TTL_SECONDS
CAP = replay_limits.REPLAY_BODY_MAX_BYTES


def test_record_ttls_share_one_default() -> None:
    assert TTL == 24 * 60 * 60
    assert idempotency_fingerprint._DEFAULT_TTL_SECONDS == TTL
    assert _dedupe.COMMAND_DEDUPE_TTL_SECONDS == TTL
    params = inspect.signature(di.idempotency_guard).parameters
    assert params["ttl_seconds"].default == TTL
    inbox = inspect.signature(ConsumerInbox.__init__).parameters
    assert inbox["ttl_seconds"].default == TTL
    assert CeleryTaskQueueOptions.model_fields["dedupe_ttl_seconds"].default == TTL
    field = Settings.model_fields["contract_command_dedupe_ttl_seconds"]
    assert field.default == TTL


def test_replay_body_caps_share_one_bound() -> None:
    assert CAP == 256 * 1024
    assert idempotency_capture._MAX_CAPTURED_RESPONSE_BYTES == CAP
    assert _dedupe.COMMAND_RESULT_REPLAY_MAX_BYTES == CAP
