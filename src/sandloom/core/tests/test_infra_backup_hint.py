"""``InfraProvider.backup_hint`` and ``BackupHint``: the driver API behind
``sandloom backup plan`` (what each server holds, said by its own driver).

The plan's merge (one item per server, keep-last steps, the tolerable-loss rule)
is tested with stand-in drivers in ``test_backup_cli.py``; the real drivers' hints
in their packages.
"""

from __future__ import annotations

import pytest

from sandloom.core.infra.ports._base import (
    BACKUP_CRITICALITIES,
    BackupHint,
    InfraProvider,
)


class _Driver(InfraProvider):
    contract_version = 1


def test_a_driver_that_says_nothing_has_no_backup_hint() -> None:
    """The base answer: no server of its own to back up (a memory driver)."""
    assert _Driver.backup_hint({"url": "stub://host:1/0"}) is None


def test_a_hint_defaults_to_the_least_severe_criticality() -> None:
    hint = BackupHint(url="stub://h:1/0", kind="stub")
    assert hint.criticality == "optional"
    assert hint.backup == () and hint.restore == ()
    assert hint.tolerable_loss is None


def test_the_criticalities_run_from_the_most_to_the_least_severe() -> None:
    assert BACKUP_CRITICALITIES == ("critical", "required", "optional")


@pytest.mark.parametrize("kind", ["", "two words"])
def test_a_hint_kind_is_one_word(kind: str) -> None:
    with pytest.raises(ValueError, match="one word"):
        BackupHint(url=None, kind=kind)


def test_a_hint_criticality_is_one_the_plan_knows() -> None:
    with pytest.raises(ValueError, match="criticality"):
        BackupHint(url=None, kind="stub", criticality="urgent")  # type: ignore[arg-type]


def test_a_hint_url_is_a_string_or_none_never_empty() -> None:
    assert BackupHint(url=None, kind="stub").url is None
    with pytest.raises(ValueError, match="url"):
        BackupHint(url="", kind="stub")
