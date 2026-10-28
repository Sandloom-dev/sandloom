"""The committed contract snapshots match the code.

A split deployment checks a saga's remote steps against the PROVIDER's
snapshot (``contracts.snapshot.json`` next to its ``__init__.py``) at
startup, and the readiness probe compares it with the live provider: a stale
snapshot must fail here first. Regenerate with
``sandloom contracts snapshot --write`` (``sandloom test`` writes it too).
"""

from sandloom.sdk.tooling import main  # sandloom: advanced the sandloom contracts CLI


def test_contract_snapshots_are_fresh() -> None:
    assert main(["snapshot", "--check"]) == 0
