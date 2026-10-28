"""The taskboard migrations install the RLS its TenantScoped models promise."""

import pytest

from sandloom.sdk.testing import (  # sandloom: advanced migration chain helpers
    migrate_module,
    run_sql,
)


@pytest.mark.parametrize(
    "table", ["taskboard_project_task_query_index", "taskboard_project_snapshot"]
)
def test_taskboard_installs_forced_tenant_rls(scratch_database, table):
    migrate_module(scratch_database, "projecthub.modules.taskboard")
    assert run_sql(
        scratch_database,
        f"SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname='{table}'",
    ) == [(True, True)]
    assert run_sql(
        scratch_database,
        f"SELECT policyname FROM pg_policies WHERE tablename='{table}'",
    ) == [(f"{table}_tenant_isolation",)]
