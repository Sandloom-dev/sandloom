"""Source module migrations install the RLS promised by their models."""

import pytest

from sandloom.sdk.testing import (  # sandloom: advanced migration chain helpers
    migrate_module,
    run_sql,
)


@pytest.mark.parametrize("module", ["projects", "tasks"])
def test_source_module_installs_forced_tenant_rls(scratch_database, module):
    migrate_module(scratch_database, f"projecthub.modules.{module}")
    table = f"{module}_{module}"
    assert run_sql(
        scratch_database,
        f"SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname='{table}'",
    ) == [(True, True)]
    assert run_sql(
        scratch_database,
        f"SELECT policyname FROM pg_policies WHERE tablename='{table}'",
    ) == [(f"{table}_tenant_isolation",)]
