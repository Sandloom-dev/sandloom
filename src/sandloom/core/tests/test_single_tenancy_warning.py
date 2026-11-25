"""A tenant authority composed under ``TENANCY_MODE=single`` warns at boot.

Single mode binds the configured tenant for every request, so the tenants a
module grants through ``tenant_ids`` are never selected. ``create_app`` logs
one WARNING naming both; no tenant authority, or ``multi``, logs none.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.app_factory import claim_authorities as _claims
from sandloom.core.claims import NO_AUTHORITIES

AUTHORITY = "acme_grants"
_MODULE = """
from sandloom.core.module_api import define_module


def _setup(api):
    pass


register = define_module(__name__, setup=_setup, events=None)
"""


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "TENANCY_MODE=single" in r.getMessage()
    ]


def test_the_warning_names_the_mode_and_the_authority() -> None:
    policy = _claims.build_claim_authorities({"tenant_ids": AUTHORITY})
    log = logging.getLogger("test")
    message = _claims.warn_single_tenancy_authority(policy, "single", log)
    assert message is not None
    assert "TENANCY_MODE=single" in message
    assert repr(AUTHORITY) in message


@pytest.mark.parametrize(
    ("tenancy_mode", "mapping"),
    [("multi", {"tenant_ids": AUTHORITY}), ("single", None)],
)
def test_no_warning_without_both(
    tenancy_mode: str, mapping: dict[str, str] | None
) -> None:
    policy = (
        NO_AUTHORITIES if mapping is None else _claims.build_claim_authorities(mapping)
    )
    log = logging.getLogger("test")
    assert _claims.warn_single_tenancy_authority(policy, tenancy_mode, log) is None


@pytest.fixture
def authority_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A module that is the composition's only one; ``mode`` sets the tenancy."""
    from sandloom.core.config.settings import settings

    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(settings, "module_load_entrypoints", False)
    package = tmp_path / AUTHORITY
    package.mkdir()
    (package / "__init__.py").write_text(_MODULE)
    monkeypatch.setattr(settings, "sandloom_modules", [AUTHORITY])
    monkeypatch.setattr(settings, "module_required", [AUTHORITY])

    def mode(tenancy_mode: str) -> None:
        monkeypatch.setattr(settings, "tenancy_mode", tenancy_mode)
        monkeypatch.setattr(settings, "single_tenant_id", uuid.uuid4())

    return mode


def test_create_app_warns_for_a_tenant_authority_under_single(
    authority_module: Any, caplog: pytest.LogCaptureFixture
) -> None:
    from sandloom.core.app_factory import create_app

    authority_module("single")
    with caplog.at_level(logging.WARNING, logger="sandloom.app"):
        create_app(claim_authorities={"tenant_ids": AUTHORITY})
    warned = _warnings(caplog)
    assert len(warned) == 1
    assert repr(AUTHORITY) in warned[0]


def test_create_app_does_not_warn_under_multi(
    authority_module: Any, caplog: pytest.LogCaptureFixture
) -> None:
    from sandloom.core.app_factory import create_app

    authority_module("multi")
    with caplog.at_level(logging.WARNING, logger="sandloom.app"):
        create_app(claim_authorities={"tenant_ids": AUTHORITY})
    assert _warnings(caplog) == []
