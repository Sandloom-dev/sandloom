"""Every module of this app boots ALONE (create_app + lifespan), or refuses
only with the documented ContractBindingError for its required contracts."""

import pytest

from sandloom.sdk.testing import (
    boot_module_alone,  # sandloom: advanced boots-alone gate
)

EXPECTED_MISSING: dict[str, frozenset[str]] = {
    "helpdesk.modules.tickets": frozenset(),
}


@pytest.mark.parametrize("module", sorted(EXPECTED_MISSING))
async def test_module_boots_alone(module: str) -> None:
    outcome = await boot_module_alone(module)
    assert outcome.missing_contracts == EXPECTED_MISSING[module]
    assert outcome.started is not EXPECTED_MISSING[module]
