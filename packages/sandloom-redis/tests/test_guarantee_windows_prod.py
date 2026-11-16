"""``create_app`` refuses a production window violation, on the Redis drivers.

Production selects the shared drivers (the Redis cache and security store, the
Streams bus), which the kernel does not ship: the startup preflight (SANDLOOM-B037)
refuses a production selection of drivers that are not installed before the
window check runs. The window rules themselves are the kernel's
(``src/sandloom/core/tests/test_guarantee_windows.py``).
"""

from __future__ import annotations

from importlib.util import find_spec

import pytest

from sandloom.core import guarantee_windows as gw

_STRONG_KEY = "9f3b7c1e5a2d4f6b8c0e1a3d5f7b9c2e4a6d8f0b1c3e5a7d9f2b4c6e8a0d1f3b"


def test_create_app_refuses_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    from sandloom.core.config import module_settings
    from sandloom.core.config.settings import settings
    from sandloom.core.tests._root_support import create_root_app

    monkeypatch.setattr(settings, "contract_command_dedupe_ttl_seconds", 5)
    monkeypatch.setattr(settings, "environment", "prod")
    # prod selects the shared Streams bus: name the service (ADR-0012 d. 5).
    monkeypatch.setattr(settings, "sandloom_service_name", "api")
    # ``.env.test`` selects the memory bus; the composition's streams bus decides.
    monkeypatch.setattr(settings, "event_bus_backend", None)
    # Production declares its security store on every profile (spec 4). The
    # rest is what a composition selects (the kernel-only venv has none).
    monkeypatch.setattr(
        settings,
        "infra",
        {
            "atomic_store": {
                "security": {"driver": "redis", "url": "redis://sec:6379/0"}
            },
            "database": {
                "default": {"driver": "sqlalchemy", "url": settings.database_url}
            },
            "cache": {"default": {"driver": "redis", "url": settings.cache_url}},
            "eventbus": {"default": {"driver": "streams"}},
        },
    )
    # The bundled modules refuse prod without these while they register,
    # before the window check runs: satisfy them so this test sees its own.
    # (identity's own settings read the process's source read: re-take it)
    monkeypatch.setenv("TWOFA_ENCRYPTION_KEY", _STRONG_KEY)
    monkeypatch.setattr(module_settings, "_PROCESS_SNAPSHOT", None)
    # billing's own risk id is known only where billing is installed (a risk
    # naming an unknown module is refused, also in the no-batteries venv).
    billing = ["billing.fake_psp_in_prod"] if find_spec("sandloom_billing") else []
    monkeypatch.setattr(
        settings,
        "sandloom_accepted_risks",
        [
            *settings.sandloom_accepted_risks,
            *billing,
            "log_notifier_in_prod",
        ],
    )
    with pytest.raises(gw.GuaranteeWindowError, match="command_dedupe"):
        create_root_app()
