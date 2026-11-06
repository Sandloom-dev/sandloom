"""The tenancy migration chain at head builds the schema its models declare.

SQLite takes its schema from the models and never runs the chain
(SANDLOOM-B028); this test is what keeps the PostgreSQL schema the chain
builds equal to them. It skips on SQLite.
"""

from sandloom.sdk.testing import assert_migrations_match_models


def test_migrations_match_models() -> None:
    assert_migrations_match_models("sandloom_tenancy")
