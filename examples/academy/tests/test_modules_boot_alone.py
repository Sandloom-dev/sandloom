"""Every module of this app boots ALONE (create_app + lifespan), or refuses
only with the documented ContractBindingError for its required contracts."""

import pytest

from sandloom.sdk.testing import (
    boot_module_alone,  # sandloom: advanced boots-alone gate
)

EXPECTED_MISSING: dict[str, frozenset[str]] = {
    # Its saga's steps call both providers: register_saga adds them to the
    # module's consumes, so it boots only where they are local or bound.
    "academy.modules.enrollment": frozenset(
        {"payments.charges.v1", "notifications.mail.v1"}
    ),
    "academy.modules.notifications": frozenset(),
    "academy.modules.payments": frozenset(),
}


@pytest.mark.parametrize("module", sorted(EXPECTED_MISSING))
async def test_module_boots_alone(module: str) -> None:
    outcome = await boot_module_alone(module)
    assert outcome.missing_contracts == EXPECTED_MISSING[module]
    assert outcome.started is not EXPECTED_MISSING[module]
