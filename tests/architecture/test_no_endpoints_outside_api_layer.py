from pathlib import Path

import pytest

from sandloom.core.archcheck.analyzers import find_endpoints_outside_api

# ANSI colors
RED = "\x1b[31m"
BOLD = "\x1b[1m]".replace("]", "")  # avoid bracket issues in some linters
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


def _iter_py_files():
    for mod in BUSINESS_MODULES:
        for py in (SANDLOOM_ROOT / mod).glob("**/*.py"):
            # Test fixtures may define routers/imports outside production rules
            if "tests" in py.parts:
                continue
            yield mod, py


@pytest.mark.parametrize("module_name,file_path", list(_iter_py_files()))
def test_no_endpoints_outside_api_layer(module_name: str, file_path: Path):
    # API files are skipped inside the shared analyzer (endpoints belong there).
    violations = find_endpoints_outside_api(
        file_path, module_root=SANDLOOM_ROOT / module_name
    )

    if violations:
        header = f"ENDPOINTS OUTSIDE API LAYER DETECTED in module '{module_name}':"
        msg = (
            f"\n\n{'=' * (len(header) + 4)}\n"
            f"{BOLD}{RED}{header}{RESET}\n"
            f"{'-' * (len(header) + 4)}\n"
            f"File: {file_path}\n"
            f"{RED}" + "\n".join(violations) + f"{RESET}\n\n"
            "WHY IS THIS FORBIDDEN?\n"
            "We keep all HTTP endpoints in the API layer (e.g., 'api/', 'routers/'). "
            "This preserves a clean architecture: services, models, and domain code "
            "must not directly depend on web/presentation concerns.\n\n"
            "HOW TO FIX THIS?\n"
            " - Move this function and its router decorator into the module's API "
            "folder.\n"
            " - Keep only DTOs and orchestration at the API layer; domain logic "
            "stays in services.\n"
            f"{'=' * (len(header) + 4)}"
        )
        pytest.fail(msg, pytrace=False)


# --- Analyzer self-test (positive detection on example fixtures) ------------
def _endpoint_violations(file_path: Path) -> list[str]:
    """Endpoints defined in a non-API file (empty for API-layer files)."""
    return find_endpoints_outside_api(file_path)


def test_analyzer_flags_endpoint_outside_api_layer() -> None:
    bm = FIXTURES_ROOT / "broken_module"
    # Endpoints in non-API files (utils, services) MUST be flagged.
    assert _endpoint_violations(bm / "utils.py.txt")
    assert _endpoint_violations(bm / "services" / "violator_service.py.txt")
    # An endpoint correctly placed in api/ MUST NOT be flagged (control).
    assert _endpoint_violations(bm / "api" / "correct_router.py.txt") == []


def test_api_directory_above_the_module_root_does_not_exempt_it() -> None:
    """``/srv/api/<module>/services/x.py`` is a service file, not the API layer.

    The fixture module ``shop`` lives under an ``api/`` directory; the layer is
    judged on the path relative to the module root (or the package chain).
    """
    shop = FIXTURES_ROOT / "api" / "shop"
    service = shop / "services" / "endpoint_in_service.py.txt"
    assert _endpoint_violations(service)
    assert find_endpoints_outside_api(service, module_root=shop)
    # Control: the same file inside the module's own api/ is fine.
    assert (
        find_endpoints_outside_api(
            FIXTURES_ROOT / "broken_module" / "api" / "correct_router.py.txt",
            module_root=FIXTURES_ROOT / "broken_module",
        )
        == []
    )


def test_sandloom_check_judges_api_layer_relative_to_module(tmp_path: Path) -> None:
    from sandloom.core.archcheck import load_rules, run_checks

    checkout = tmp_path / "srv" / "api"  # an `api` dir ABOVE the app
    pkg = checkout / "acme" / "modules"
    (pkg / "shop" / "services").mkdir(parents=True)
    (pkg / "shop" / "api").mkdir()
    for d in (checkout / "acme", pkg, pkg / "shop", pkg / "shop" / "services"):
        (d / "__init__.py").write_text("")
    source = (
        FIXTURES_ROOT / "api" / "shop" / "services" / "endpoint_in_service.py.txt"
    ).read_text()
    (pkg / "shop" / "services" / "orders.py").write_text(source)
    (pkg / "shop" / "api" / "orders.py").write_text(source)
    manifest = checkout / "architecture_rules.yml"
    manifest.write_text("modules_path: acme/modules\nmodules_package: acme.modules\n")
    violations = run_checks(load_rules(manifest), only=["endpoints-outside-api"])
    assert [v.file.relative_to(pkg).as_posix() for v in violations] == [
        "shop/services/orders.py"
    ]
