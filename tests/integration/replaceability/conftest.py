"""The replaceability fixtures' shared ``infra`` fixture.

Defined in ``test_replaceability.py``; offered here so ``test_trust_rows.py``
uses it without importing a fixture by name (each test module still gets its
own database: the fixture is module-scoped).
"""

from test_replaceability import infra  # noqa: F401 - the module-scoped fixture
