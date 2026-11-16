"""The worker host is the Celery driver's, end to end against the suite's root."""

from __future__ import annotations

from sandloom.core import worker


def test_real_composition_root_resolves_driver_built_host() -> None:
    """The host is the taskqueue driver's and the boot derives the table of the
    loaded modules."""
    core = worker.load_composition_core()
    provider, host = worker.resolve_worker_host(core, "default")
    assert host is not None and hasattr(host, "run_worker")
    table = worker.KernelWorkerBoot(core, provider, "default").boot()
    if "sandloom_identity" in core.loaded_modules:
        assert "sandloom_identity.tasks.audit_user_activity" in table.tasks
