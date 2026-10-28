"""Architecture rule: business modules cannot reach the RLS system channel.

With ``tenant_rls`` the ordinary application role cannot bypass Row-Level
Security; privileged cross-tenant work runs on a separate engine logged in as a
separate role (``sandloom.core.drivers.system_db``). That engine must not be
obtainable by business-module code: not through the public core API, not
through a provider attribute, not through a static or dynamic import.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from sandloom.core.archcheck.analyzers import (
    FORBIDDEN_INFRA_MODULES,
    find_legacy_infra_imports,
)
from sandloom.core.archcheck.manifest import load_rules

BACKEND = Path(__file__).resolve().parents[2]

PUBLIC_MODULES = (
    "sandloom.core.di",
    "sandloom.core.facade",
    "sandloom.core.module_api",
    "sandloom.core.descriptors",
    "sandloom.core.orm",
    "sandloom.core.projections",
)
SYSTEM_NAMES = {"system_session", "get_system_engine", "SystemChannelUnavailable"}


def test_system_channel_modules_are_forbidden_infra_modules() -> None:
    assert "sandloom.core.sql_alchemy.tenant_rls" in FORBIDDEN_INFRA_MODULES
    # the system channel lives in the driver tree:
    assert "sandloom.core.drivers.system_db".startswith("sandloom.core.drivers.")
    assert "sandloom.core.drivers" in FORBIDDEN_INFRA_MODULES


def test_system_channel_not_in_public_core_api() -> None:
    manifest = load_rules(BACKEND / "architecture_rules.yml")
    public = set(manifest.public_api)
    assert not any("system_db" in p or "sql_alchemy" in p for p in public)
    for mod in PUBLIC_MODULES:
        exported = set(dir(importlib.import_module(mod)))
        assert not (exported & SYSTEM_NAMES), (mod, exported & SYSTEM_NAMES)


@pytest.mark.parametrize(
    "source",
    [
        "from sandloom.core.drivers.system_db import system_session\n",
        "import sandloom.core.drivers.system_db\n",
        "from sandloom.core.sql_alchemy.tenant_rls import install_tenant_rls\n",
        "from sandloom.core.drivers.sqlalchemy_database import get_system_engine\n",
        "import importlib\nimportlib.import_module('sandloom.core.drivers.system_db')\n",
        "__import__('sandloom.core.drivers.sqlalchemy_database')\n",
    ],
)
def test_module_import_of_system_channel_is_flagged(
    tmp_path: Path, source: str
) -> None:
    f = tmp_path / "service.py"
    f.write_text(source)
    assert find_legacy_infra_imports(f), source


def test_provider_does_not_expose_system_engine() -> None:
    from sandloom.core.drivers.sqlalchemy_database import SQLAlchemyDatabaseProvider

    members = {n for n in dir(SQLAlchemyDatabaseProvider) if "system" in n.lower()}
    assert members == set(), members
