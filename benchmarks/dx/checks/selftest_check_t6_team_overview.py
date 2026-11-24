"""Self-test of ``check_t6_team_overview``'s billing source rule (not a hidden
check: ``run_eval.py`` runs only ``check_<task>.py``). Run it from this
directory: ``uv run python -m pytest -q -c pytest.ini
selftest_check_t6_team_overview.py``."""

import os
import tempfile

import pytest

os.environ.setdefault("DX_APP", tempfile.mkdtemp())  # the checker reads it at import

import check_t6_team_overview as check  # noqa: E402


@pytest.mark.parametrize(
    ("source", "accepted"),
    [
        ('SOURCES = ["billing.subscription.created.v1"]', True),
        (
            "from teamhub.shared.contracts.billing_entitlements_v1 import "
            "BILLING_ENTITLEMENTS_V1\n",
            True,
        ),
        ("from .billing_entitlements_v1 import BILLING_ENTITLEMENTS_V1", True),
        ("# the plan comes from billing.entitlements.v1, not wired yet", False),
        ("plan = 'free'  # no billing source at all", False),
    ],
)
def test_billing_is_a_subscription_projection_or_a_live_entitlements_read(
    source: str, accepted: bool
) -> None:
    assert check.reads_billing(source) is accepted
