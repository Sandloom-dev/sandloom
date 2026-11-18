"""``sandloom generate --new-major``: which source directory a name resolves
to, and the next major's name (ADR-0017)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sandloom.core.cli import generated
from sandloom.core.cli.new_major import NewMajorError, next_major_name


def test_a_module_of_a_tree_of_top_level_packages_resolves_to_its_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In a tree of top-level packages the module ``widgets`` is the directory
    ``src/acme_widgets`` (its package's entry-point name is ``widgets``), not a
    ``src/widgets`` that does not exist."""
    rules = SimpleNamespace(
        modules_path=tmp_path / "src",
        business_modules=lambda: ["acme_widgets", "acme_gadgets"],
        module_package=lambda directory: directory,
    )
    names = {"acme_widgets": "widgets", "acme_gadgets": "gadgets"}
    monkeypatch.setattr(generated, "module_name", lambda package: names[package])
    found = generated._module_directory(rules, "widgets")
    assert found == tmp_path / "src" / "acme_widgets"
    assert generated._module_directory(rules, "acme_widgets") == found
    assert generated._module_directory(rules, "gadgets") == (
        tmp_path / "src" / "acme_gadgets"
    )
    assert generated._module_directory(rules, "no_such_module") is None


def test_the_next_major_is_one_up_and_a_name_without_a_major_is_refused() -> None:
    assert next_major_name("tenancy.membership.v1") == "tenancy.membership.v2"
    assert next_major_name("notes.note.archived.v9") == "notes.note.archived.v10"
    for bad in ("tenancy.membership", "tenancy", "tenancy.membership.v"):
        with pytest.raises(NewMajorError, match="not a versioned"):
            next_major_name(bad)
