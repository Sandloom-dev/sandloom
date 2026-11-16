"""Every copy of ``DEFAULT_INFRA`` equals the standard one.

``sandloom_standard.DEFAULT_INFRA`` is the drivers a Sandloom app runs on. The
``sandloom new`` scaffold and each example app carry a literal copy (the app
names its drivers in its own package, ``create_app(default_infra=...)``). A copy
that drifts selects other drivers than the docs say, and nothing else compares
them. Each copy is read with ``ast`` (no import of an example app).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
#: Read as source, not imported: the kernel-without-batteries lane runs this
#: suite without ``sandloom-standard`` installed.
STANDARD = (
    REPO
    / "packages"
    / "sandloom-standard"
    / "src"
    / "sandloom_standard"
    / "__init__.py"
)
SCAFFOLD = REPO / "src" / "sandloom" / "core" / "cli" / "project.py"
EXAMPLE_PACKAGES = sorted(REPO.glob("examples/*/*/__init__.py"))

#: An example that deliberately selects other drivers: path (relative to the
#: repository) -> the reason. None does today.
DELIBERATE_DIFFERENCES: dict[str, str] = {}

#: Examples that carry a copy today: a new one is picked up by the glob and
#: must be listed here (it is then compared too).
EXPECTED_EXAMPLES = {
    "examples/academy/academy/__init__.py",
    "examples/helpdesk/helpdesk/__init__.py",
    "examples/linkly/linkly/__init__.py",
    "examples/oidc-identity/oidc_identity/__init__.py",
    "examples/projecthub/projecthub/__init__.py",
    "examples/tutorial/tasklist/__init__.py",
}


def _literal_of(tree: ast.AST) -> object | None:
    """The value of the ``DEFAULT_INFRA = {...}`` assignment of ``tree``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "DEFAULT_INFRA" for t in node.targets
        ):
            return ast.literal_eval(node.value)
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "DEFAULT_INFRA"
            and node.value is not None
        ):
            return ast.literal_eval(node.value)
    return None


def module_copy(source: str) -> object | None:
    """The copy a module's source carries, ``None`` when it has none."""
    return _literal_of(ast.parse(source))


def scaffold_copy(source: str) -> object | None:
    """The copy the scaffold template (a string constant of ``project.py``)
    writes into a new app's ``__init__.py``."""
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "\nDEFAULT_INFRA = {" in node.value
        ):
            return module_copy(node.value)
    return None


DEFAULT_INFRA = module_copy(STANDARD.read_text())


def test_the_standard_default_infra_is_not_empty() -> None:
    # Bite: a comparison against an empty reference would pass an empty copy.
    assert isinstance(DEFAULT_INFRA, dict) and DEFAULT_INFRA
    assert DEFAULT_INFRA["eventbus"]["default"]["driver"] == "streams"


def test_the_scaffold_copy_equals_the_standard_one() -> None:
    copy = scaffold_copy(SCAFFOLD.read_text())
    assert copy is not None, f"no DEFAULT_INFRA literal found in {SCAFFOLD}"
    assert copy == DEFAULT_INFRA


def test_the_examples_found_are_the_ones_expected() -> None:
    carrying = {
        str(path.relative_to(REPO))
        for path in EXAMPLE_PACKAGES
        if module_copy(path.read_text()) is not None
    }
    assert carrying == EXPECTED_EXAMPLES, (
        "an example gained or lost a DEFAULT_INFRA copy: list it in "
        "EXPECTED_EXAMPLES (it is then compared with the standard one)"
    )


@pytest.mark.parametrize("relative", sorted(EXPECTED_EXAMPLES))
def test_an_example_copy_equals_the_standard_one(relative: str) -> None:
    copy = module_copy((REPO / relative).read_text())
    if relative in DELIBERATE_DIFFERENCES:
        assert copy != DEFAULT_INFRA, (
            f"{relative} no longer differs: drop its DELIBERATE_DIFFERENCES entry"
        )
        return
    assert copy == DEFAULT_INFRA


def test_the_comparison_sees_a_drifted_copy() -> None:
    # Bite: each reader returns what it parsed, so a changed driver, a missing
    # capability and a missing literal are all told apart from the real thing.
    real = "DEFAULT_INFRA = " + repr(DEFAULT_INFRA) + "\n"
    assert module_copy(real) == DEFAULT_INFRA
    assert module_copy(real.replace("'streams'", "'memory'")) != DEFAULT_INFRA
    assert module_copy(real.replace("'lease'", "'lease2'")) != DEFAULT_INFRA
    assert module_copy("OTHER = {}\n") is None
    annotated = real.replace("DEFAULT_INFRA =", "DEFAULT_INFRA: dict[str, object] =")
    assert module_copy(annotated) == DEFAULT_INFRA
    template = f"_INIT = Template('''x\n{real}''')\n"
    assert scaffold_copy(template) == DEFAULT_INFRA
    assert scaffold_copy("X = 1\n") is None
