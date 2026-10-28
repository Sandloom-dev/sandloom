from pathlib import Path

import pytest

from sandloom.core.archcheck.analyzers import find_orm_leakage

# ANSI colors
RED = "\x1b[31m"
BOLD = "\x1b[1m"
RESET = "\x1b[0m"

# Project root
SANDLOOM_ROOT = Path(__file__).resolve().parents[2] / "src" / "sandloom"

# Deliberately-broken example fixtures (data files OUTSIDE the package) used by
# the positive-detection self-test below.
FIXTURES_ROOT = Path(__file__).parent / "_fixtures"

# Business modules
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

# Legacy violations tracked for cleanup. Ratchet rule: entries may only be
# removed (after refactoring the file to DTO-based endpoints), never added.
# Legacy ORM-leakage debt ratchet: entries may be REMOVED (once a file is
# refactored to keep ORM out of its endpoints), never ADDED. Now empty — the
# identity API layer no longer leaks ORM models into endpoints.
ORM_LEAKAGE_BASELINE: set[str] = set()


def _iter_py_files():
    for mod in BUSINESS_MODULES:
        for py in (SANDLOOM_ROOT / mod).glob("**/*.py"):
            # Test fixtures may define routers/imports outside production rules
            if "tests" in py.parts:
                continue
            yield mod, py


@pytest.mark.parametrize("module_name,file_path", list(_iter_py_files()))
def test_api_no_orm_leakage_in_endpoints(module_name: str, file_path: Path):
    # Shared analyzer: only files that define endpoints are judged; ORM model
    # / SQLAlchemy imports there and ORM types in endpoint signatures or
    # bodies are violations.
    all_violations = find_orm_leakage(file_path)

    rel_path = str(file_path.relative_to(SANDLOOM_ROOT))
    if all_violations and rel_path in ORM_LEAKAGE_BASELINE:
        pytest.xfail(
            f"known legacy ORM leakage in {rel_path}; tracked in ORM_LEAKAGE_BASELINE"
        )

    if all_violations:
        header = f"API ORM LEAKAGE DETECTED in endpoints of module '{module_name}':"
        msg = (
            f"\n\n{'=' * (len(header) + 4)}\n"
            f"{BOLD}{RED}{header}{RESET}\n"
            f"{'-' * (len(header) + 4)}\n"
            f"File: {file_path}\n"
            f"{RED}" + "\n".join(all_violations) + f"{RESET}\n\n"
            "WHY IS THIS FORBIDDEN?\n"
            "Endpoint contracts must be stable and independent from persistence. "
            "ORM models (SQLAlchemy) are internal details and must not leak to API.\n\n"
            "HOW TO FIX THIS?\n"
            " - Use Pydantic DTOs for request/response (preferably in 'sandloom/shared/"
            "schemas').\n"
            " - Map ORM -> DTO in services/handlers before returning from endpoint.\n"
            " - Keep SQLAlchemy usage and models imports out of endpoint files.\n"
            f"{'=' * (len(header) + 4)}"
        )
        pytest.fail(msg, pytrace=False)


# --- Analyzer self-test (positive detection on the example fixture) ---------
def _orm_leakage_violations(file_path: Path) -> list[str]:
    return find_orm_leakage(file_path)


def test_analyzer_flags_orm_leakage_in_endpoints() -> None:
    fixture = FIXTURES_ROOT / "broken_module" / "api" / "orm_leaks_router.py.txt"
    assert _orm_leakage_violations(fixture), (
        "ORM-leakage analyzer found nothing in the leak fixture — lost coverage."
    )
