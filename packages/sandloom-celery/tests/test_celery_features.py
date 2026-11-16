"""The features the ``celery`` driver advertises against the kernel's taskqueue
capability (``sandloom.core.tests.test_module_capability_derivation`` holds the
kernel's half: which features the capability names and how a requirement meets
a driver)."""

from __future__ import annotations

from sandloom_celery.taskqueue import CELERY_TASKQUEUE_FEATURES

from sandloom.core.infra.capabilities import TASKQUEUE_CAPABILITY


def test_celery_advertises_periodic_and_only_capability_features() -> None:
    assert "periodic" in CELERY_TASKQUEUE_FEATURES
    assert CELERY_TASKQUEUE_FEATURES <= TASKQUEUE_CAPABILITY.features


def test_celery_does_not_advertise_idempotency_key() -> None:
    """``idempotency_key`` is the kernel layer's, no longer a driver feature."""
    assert "idempotency_key" not in CELERY_TASKQUEUE_FEATURES
