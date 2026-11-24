"""t6_team_overview — cross-module read model (projection)."""

import ast

from _dx import module_dir, module_tests, routes, source_files, text

TEAM = module_dir("team_overview")


def test_fn_list_endpoint() -> None:
    assert any(r.method == "GET" for r in routes(TEAM))


def test_fn_tests_written() -> None:
    assert module_tests(TEAM)


def test_dev_projection_not_rpc_join() -> None:
    """A read spanning modules is a projection built from events."""
    src = text(TEAM)
    assert "Projection(" in src or "register_projection_consumer" in src


def test_dev_version_guarded_writes() -> None:
    """Rows converge under reordering: a version per aggregate guards each write
    (a projection's ``version_field=``, ``monotonic_upsert``, or the shared
    tenancy membership guard ``member_event_action(`` from
    ``sandloom.shared.membership``)."""
    src = text(TEAM)
    assert (
        "version_field=" in src
        or "monotonic_upsert" in src
        or "member_event_action(" in src
    )


def test_dev_rebuildable() -> None:
    src = text(TEAM)
    assert "rebuild_from=" in src or "register_projection_rebuilder" in src


STUB = "billing_entitlements_v1"


def imports_stub(source: str) -> bool:
    """Does the file execute an import of the generated contract stub module
    ``billing_entitlements_v1``: ``from <pkg>.billing_entitlements_v1 import
    X``, ``from <pkg> import billing_entitlements_v1`` or ``import
    <pkg>.billing_entitlements_v1 [as b]``? Read from the syntax tree, so a
    comment or a docstring that shows the line does not count; a file that
    does not parse imports nothing."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[-1] == STUB:
                return True
            if any(alias.name == STUB for alias in node.names):
                return True
        elif isinstance(node, ast.Import):
            if any(alias.name.split(".")[-1] == STUB for alias in node.names):
                return True
    return False


def reads_billing(sources: list[str]) -> bool:
    """The billing side of the overview comes from billing's own data: a
    projection over ``billing.subscription`` events, or a live read of the
    plan through the ``billing.entitlements.v1`` contract at request time (the
    recipe ``projection-from-platform-events``: the subscription events carry
    no version, so the plan is read live). ``sources`` are the module's files;
    the live read counts only as an import of the contract stub."""
    return any("billing.subscription" in s for s in sources) or any(
        imports_stub(s) for s in sources
    )


def test_dev_sources_both_modules() -> None:
    sources = [p.read_text(errors="replace") for p in source_files(TEAM)]
    assert "tenancy.member" in "\n".join(sources)
    assert reads_billing(sources)
