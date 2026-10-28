"""Business module schemas must not import ORM/persistence modules.

Schemas/DTOs are allowed to be imported by API clients, documentation tooling
and other non-persistence code. Pulling ``sandloom.<module>.models`` into them drags
SQLAlchemy and the ORM base across that boundary.
"""

from __future__ import annotations

from pathlib import Path

from sandloom.core.archcheck import load_rules, run_checks
from sandloom.core.archcheck.analyzers import (
    find_schema_orm_imports as _schema_import_violations,
)

SANDLOOM_ROOT = Path(__file__).resolve().parents[2] / "src" / "sandloom"
FIXTURES = Path(__file__).parent / "_fixtures"

BUSINESS_MODULES = [
    d.name
    for d in SANDLOOM_ROOT.iterdir()
    if (
        d.is_dir()
        and (d / "__init__.py").exists()
        and not d.name.startswith(("_", "."))
        and d.name not in ("core", "shared", "sdk")
    )
]


def test_business_module_schemas_do_not_import_orm() -> None:
    offences: list[str] = []
    for module_name in BUSINESS_MODULES:
        base = SANDLOOM_ROOT / module_name
        files = sorted((base / "schemas").rglob("*.py"))
        if (base / "schemas.py").is_file():
            files.append(base / "schemas.py")
        for py_file in files:
            found = _schema_import_violations(py_file)
            if found:
                offences.append(f"\nIn {py_file}:\n" + "\n".join(found))

    assert not offences, (
        "Business module schemas must not import ORM models or SQLAlchemy. "
        "Move shared enums/constants to a persistence-free module and import "
        "that from both schemas and models.\n" + "".join(offences)
    )


def test_analyzer_detects_schema_orm_import(tmp_path: Path) -> None:
    sample = tmp_path / "schema.py"
    sample.write_text(
        "from sandloom_identity.models.external_identity import Provider\n"
        "from sqlalchemy import String\n"
    )
    found = _schema_import_violations(sample)
    assert len(found) == 2, found


def test_analyzer_detects_every_models_spelling() -> None:
    found = _schema_import_violations(
        FIXTURES / "broken_module" / "schemas" / "orm_leak.py.txt"
    )
    joined = "\n".join(found)
    for shape in (
        "from .. import models",
        "from sandloom_identity import models",
        "from ..models.order import Order",
        "import_module('sandloom_billing.models')",
    ):
        assert shape in joined, (shape, found)
    assert "Money" not in joined
    assert len(found) == 4, found


def test_single_file_schemas_module_is_checked(tmp_path: Path) -> None:
    """``schemas.py`` (no package) is a module's DTO layer too."""
    fixture = FIXTURES / "acme" / "modules" / "orders" / "schemas.py.txt"
    found = _schema_import_violations(fixture)
    assert len(found) == 3, found

    pkg = tmp_path / "acme" / "modules"
    (pkg / "orders").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "orders" / "__init__.py").write_text("")
    (pkg / "orders" / "schemas.py").write_text(fixture.read_text())
    manifest = tmp_path / "architecture_rules.yml"
    manifest.write_text("modules_path: acme/modules\nmodules_package: acme.modules\n")
    violations = run_checks(load_rules(manifest), only=["schema-orm-imports"])
    assert len(violations) == 3, violations
    assert {v.file.name for v in violations} == {"schemas.py"}
