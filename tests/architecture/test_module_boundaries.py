from pathlib import Path

import pytest

from sandloom.core.archcheck.analyzers import find_cross_module_imports

# ANSI colors
RED = "\x1b[31m"
YELLOW = "\x1b[33m"
BOLD = "\x1b[1m"
RESET = "\x1b[0m"

# The source root: the standard modules are top-level packages next to the
# kernel (src/sandloom_identity, ...), like any ecosystem module.
SANDLOOM_ROOT = Path(__file__).resolve().parents[2] / "src"

# Define the allowed dependencies for business modules
ALLOWED_IMPORTS = {"sandloom.core", "sandloom.shared"}

# Deliberately-broken example fixtures (data files OUTSIDE the package) the
# analyzer self-tests below scan to prove violations are caught.
FIXTURES_ROOT = Path(__file__).parent / "_fixtures"
FIXTURE_MODULES = ("boards", "broken_module")

# List of all modules that are considered "business modules"
# This list is generated automatically by scanning the directory.
BUSINESS_MODULES = [
    d.name
    for d in SANDLOOM_ROOT.iterdir()
    if (
        d.is_dir()
        and (d / "__init__.py").exists()
        and not d.name.startswith(("_", "."))
        and d.name != "sandloom"  # the kernel
    )
]


def get_forbidden_imports(file_path: Path, current_module: str) -> list[str]:
    """Forbidden cross-module imports in a file (shared ``sandloom check`` analyzer)."""
    return find_cross_module_imports(
        file_path, current_module, BUSINESS_MODULES, modules_package=""
    )


# Use pytest.mark.parametrize to create a test for each business module
@pytest.mark.parametrize("module_name", BUSINESS_MODULES)
def test_module_has_no_forbidden_imports(module_name: str):
    """
    This test ensures that a business module does not directly import from another
    business module, allowing imports only from 'core' and 'shared'.
    """
    module_path = SANDLOOM_ROOT / module_name
    all_forbidden_imports: list[str] = []

    for py_file in module_path.glob("**/*.py"):
        # Module tests may exercise cross-module integration scenarios;
        # boundary enforcement targets production code only.
        if "tests" in py_file.parts:
            continue
        forbidden = get_forbidden_imports(py_file, module_name)
        if forbidden:
            all_forbidden_imports.append(
                f"\nIn file {py_file}:\n" + "\n".join(forbidden)
            )

    if all_forbidden_imports:
        # Construct a detailed, instructional error message to improve DX
        error_header = f"ARCHITECTURAL RULE VIOLATION in module '{module_name}':"
        line_separator = "-" * (len(error_header) + 4)

        reasons = (
            "\nWHY IS THIS FORBIDDEN?\n"
            "Direct imports between business modules ('identity', 'tenancy', etc.) "
            "are not allowed.\n"
            "This rule ensures that our modules remain isolated and can be scaled "
            "or separated into microservices in the future. All shared code must "
            "go through 'sandloom/core' or 'sandloom/shared'.\n\n"
            "HOW TO FIX THIS?\n"
            "Instead of importing the implementation, use a shared contract:\n"
            " - If you need to pass data, use or create a DTO in 'sandloom/shared/"
            "schemas'.\n"
            " - If you need to trigger an action, publish an event with "
            "'publish_event(...)' from 'sandloom.core.di' (transactional outbox) and "
            "react to it with 'core.register_event_consumer(...)' — never "
            "'core.event_bus'.\n"
            " - If you need a shared guard, use it from 'sandloom/shared/guards'.\n"
            " - If you need to call a service, resolve its versioned contract "
            "with 'core.get_service(\"<module>.<contract>.v1\")' (the provider "
            "runs in its own Unit of Work, locally or over RPC).\n"
        )

        forbidden_code_details = "".join(all_forbidden_imports)

        # Colorize header and details
        colored_header = f"{BOLD}{RED}{error_header}{RESET}"
        attempt_line = f"{RED}You are attempting a direct import from another business module.{RESET}"
        colored_details = f"{RED}{forbidden_code_details.strip()}{RESET}"

        full_error_message = (
            f"\n\n{'=' * (len(error_header) + 4)}\n"
            f"{colored_header}\n"
            f"{line_separator}\n"
            f"{attempt_line}\n"
            f"{colored_details}\n\n"
            f"{reasons}\n"
            f"{'=' * (len(error_header) + 4)}"
        )

        pytest.fail(full_error_message, pytrace=False)


# --- Analyzer self-tests -----------------------------------------------------
# The fixture modules below contain deliberate violations of every pattern the
# analyzer must catch. They are excluded from the enforcement parametrization
# above; these tests prove the analyzer still detects them.


@pytest.mark.parametrize("fixture_module", FIXTURE_MODULES)
def test_analyzer_detects_violations_in_fixture_modules(fixture_module: str) -> None:
    module_path = FIXTURES_ROOT / fixture_module
    assert module_path.exists(), f"missing analyzer fixture {fixture_module}"

    found: list[str] = []
    for py_file in module_path.glob("**/*.py.txt"):
        found.extend(get_forbidden_imports(py_file, fixture_module))

    assert found, (
        f"Analyzer found no forbidden imports in fixture module "
        f"'{fixture_module}' — the boundary checker has lost coverage."
    )
