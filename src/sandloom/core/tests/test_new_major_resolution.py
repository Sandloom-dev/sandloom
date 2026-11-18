"""``sandloom generate --new-major``: which source directory a name resolves
to, and the next major's name (ADR-0017)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sandloom.core.archcheck.manifest import load_rules
from sandloom.core.cli import generated
from sandloom.core.cli.new_major import NewMajorError, next_major_name

REPOSITORY = Path(__file__).resolve().parents[4]


def test_a_module_of_a_tree_of_top_level_packages_resolves_to_its_package() -> None:
    """In this repository ``tenancy`` is ``src/sandloom_tenancy``, not a
    ``src/tenancy`` that does not exist."""
    rules = load_rules(REPOSITORY / "architecture_rules.yml")
    found = generated._module_directory(rules, "tenancy")
    assert found == REPOSITORY / "src" / "sandloom_tenancy"
    assert found is not None and found.is_dir()
    assert generated._module_directory(rules, "sandloom_tenancy") == found
    assert generated._module_directory(rules, "no_such_module") is None


def test_the_next_major_is_one_up_and_a_name_without_a_major_is_refused() -> None:
    assert next_major_name("tenancy.membership.v1") == "tenancy.membership.v2"
    assert next_major_name("notes.note.archived.v9") == "notes.note.archived.v10"
    for bad in ("tenancy.membership", "tenancy", "tenancy.membership.v"):
        with pytest.raises(NewMajorError, match="not a versioned"):
            next_major_name(bad)
