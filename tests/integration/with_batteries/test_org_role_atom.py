"""Tenancy's ``OrgRole`` atom, as a consumer imports it from its stub."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from sandloom.core.cli.generated import render_stub
from sandloom_tenancy.contracts import MembershipReadContract


def test_invalid_org_role_fails_at_configuration_time(tmp_path: Path) -> None:
    """``tenancy.membership.v1``'s stub as ``sandloom generate`` writes it
    (tenancy is the provider and has none of its own)."""
    path = tmp_path / "tenancy_membership_v1.py"
    path.write_text(render_stub("tenancy.membership.v1", MembershipReadContract))
    spec = importlib.util.spec_from_file_location("_org_role_atom_stub", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        with pytest.raises(ValueError, match="member, admin, owner"):
            module.OrgRole("admn")
    finally:
        sys.modules.pop(spec.name, None)
