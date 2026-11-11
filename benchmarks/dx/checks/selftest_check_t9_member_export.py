"""Self-test of ``check_t9_member_export``'s gate parsing (not a hidden check:
``run_eval.py`` runs only ``check_<task>.py``). Run it from this directory:
``uv run python -m pytest -q -c pytest.ini selftest_check_t9_member_export.py``."""

import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("DX_APP", tempfile.mkdtemp())  # the checker reads it at import

import check_t9_member_export as check  # noqa: E402
from _dx import routes  # noqa: E402

GATES = {
    "t9": 'require(OrgRole("admin") & SubscriptionActive(of=Account.bound_tenant()))',
    "owner": "require(OrgRole(role='owner') & SubscriptionActive(of=Account.bound_tenant()))",
    "require_org_role": (
        'require(require_org_role("admin") & SubscriptionActive(of=Account.bound_tenant()))'
    ),
    "member": 'require(OrgRole("member") & SubscriptionActive(of=Account.bound_tenant()))',
    "or": 'require(OrgRole("admin") | SubscriptionActive(of=Account.bound_tenant()))',
    "user": 'require(OrgRole("admin") & SubscriptionActive(of=Account.user(reason="x")))',
    "no_role": "require(SubscriptionActive(of=Account.bound_tenant()))",
    "credits_bound": (
        'require(OrgRole("admin") & Credits(1, of=Account.bound_tenant())'
        ' & SubscriptionActive(of=Account.user(reason="x")))'
    ),
    "credits_org": (
        'require(OrgRole("admin") & Credits(1, of=Account.org(param=None))'
        ' & SubscriptionActive(of=Account.user(reason="x")))'
    ),
}


def _route(tmp_path: Path, gate: str, *, docstring: str = "") -> bool:
    (tmp_path / "api.py").write_text(
        f"PAID_ADMIN = Depends({gate})\n"
        "\n"
        '@router.get("/invitations/export", dependencies=[PAID_ADMIN])\n'
        f"async def export() -> None:\n    {docstring!r}\n"
    )
    (route,) = routes(tmp_path)
    return check._org_account(route, tmp_path)


@pytest.mark.parametrize(
    ("gate", "accepted"),
    [
        ("t9", True),
        ("owner", True),
        ("require_org_role", False),
        ("member", False),
        ("or", False),
        ("user", False),
        ("no_role", False),
        ("credits_bound", False),
        ("credits_org", False),
    ],
)
def test_bound_tenant_counts_only_under_an_org_admin_role(
    tmp_path: Path, gate: str, accepted: bool
) -> None:
    assert _route(tmp_path, GATES[gate]) is accepted


def test_a_docstring_naming_the_role_is_not_a_gate(tmp_path: Path) -> None:
    assert not _route(tmp_path, GATES["no_role"], docstring='OrgRole("admin")')


def test_the_org_account_forms_pass(tmp_path: Path) -> None:
    gate = 'require(OrgRole("admin") & SubscriptionActive(of=Account.org(param=None)))'
    assert _route(tmp_path, gate)


def test_a_multiline_gate_constant_parses(tmp_path: Path) -> None:
    (tmp_path / "api.py").write_text(
        "GATE = (\n"
        '    OrgRole("admin")\n'
        "    & SubscriptionActive(of=Account.bound_tenant())\n"
        ")\n"
        "\n"
        '@router.get("/invitations/export", dependencies=[Depends(require(GATE))])\n'
        "async def export() -> None: ...\n"
    )
    (route,) = routes(tmp_path)
    assert check._org_account(route, tmp_path)


def test_a_same_named_method_lends_no_gate(tmp_path: Path) -> None:
    (tmp_path / "api.py").write_text(
        "class Other:\n"
        f"    def export(self, _=Depends({GATES['t9']})) -> None: ...\n"
        "\n"
        f'@router.get("/invitations/export", dependencies=[Depends({GATES["no_role"]})])\n'
        "async def export() -> None: ...\n"
    )
    (route,) = routes(tmp_path)
    assert not check._org_account(route, tmp_path)


@pytest.mark.parametrize(
    "docstring",
    [
        'Account.org(param="org_id")',
        'Entitled("organization.subscription.active", owner="billing")',
    ],
)
def test_org_forms_in_text_are_not_a_gate(tmp_path: Path, docstring: str) -> None:
    assert not _route(tmp_path, GATES["user"], docstring=docstring)


@pytest.mark.parametrize(
    "gate",
    [
        'require(OrgRole("admin") | SubscriptionActive(of=Account.org(param=None)))',
        'require(OrgRole("admin") | Entitled("organization.subscription.active", owner="billing"))',
    ],
)
def test_org_forms_in_an_alternative_are_not_a_gate(tmp_path: Path, gate: str) -> None:
    assert not _route(tmp_path, gate)


def test_the_org_fact_passes(tmp_path: Path) -> None:
    assert _route(
        tmp_path,
        'require(OrgRole("admin") & Entitled("organization.subscription.active", '
        'owner="billing"))',
    )


def test_the_org_fact_of_another_owner_is_not_billing(tmp_path: Path) -> None:
    assert not _route(
        tmp_path,
        'require(OrgRole("admin") & Entitled("organization.subscription.active", '
        'owner="rogue"))',
    )


def test_a_router_level_gate_counts(tmp_path: Path) -> None:
    (tmp_path / "api.py").write_text(
        "router = module_router(dependencies=[Depends(require("
        'SubscriptionActive(of=Account.org(param="org_id"))))])\n'
        "\n"
        '@router.get("/invitations/export")\n'
        "async def export() -> None: ...\n"
    )
    (route,) = routes(tmp_path)
    assert check._org_account(route, tmp_path)
