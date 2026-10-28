"""Every Unit of Work ends through ONE commit pipeline (ADR-0005 §3).

``sandloom.core.unit_of_work.COMMIT_PIPELINE``: before_commit -> COMMIT -> outbox
dispatch -> on_commit. The stages used to be re-assembled at several call
sites (and their order drifted); now only the UoW contract runs stage 1 and
only :func:`_dispatch_outbox_after_commit` runs stages 3-4. This test keeps a
new hand-assembled variant from appearing.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "sandloom"
#: the Unit of Work engine package owns every stage
OWNER = SRC / "core" / "unit_of_work"

#: stage helper -> the runtime files (besides the unit_of_work package) allowed to call it
ALLOWED: dict[str, set[str]] = {
    # stage 1: the UoW contract, right before its COMMIT; the test seams
    # standing in for a commit
    "_run_before_commit_callbacks": {
        "core/sql_alchemy/uow.py",
        "core/di.py",
        "core/testing/_uow.py",
    },
    # stages 3-4: every commit path that does not go through open_uow
    "_dispatch_outbox_after_commit": {
        "core/module_api/_call.py",
        "core/di.py",
        "core/testing/_uow.py",
    },
    # internals of stages 1, 3 and 4: never called from outside
    "_run_on_commit_callbacks": set(),
    "_dispatch_pending_events": set(),
    "_run_request_commit_checks": set(),
}
#: an awaited ``.commit()`` (an async session / UoW commit)
AWAITED_COMMIT = re.compile(r"\bawait\s+[\w.()]*\.commit\(\)")
COMMIT_OWNER = "core/sql_alchemy/uow.py"


def _runtime_sources() -> list[Path]:
    return [
        path
        for path in SRC.rglob("*.py")
        if OWNER not in path.parents and "tests" not in path.relative_to(SRC).parts
    ]


def test_pipeline_stages_are_reached_only_through_their_owners() -> None:
    offenders: list[str] = []
    for path in _runtime_sources():
        rel = path.relative_to(SRC).as_posix()
        source = path.read_text()
        for name, allowed in ALLOWED.items():
            if rel in allowed:
                continue
            for lineno, line in enumerate(source.splitlines(), 1):
                if re.search(rf"\b{name}\b", line):
                    offenders.append(f"{rel}:{lineno}: {name}")
    assert offenders == [], (
        "commit-pipeline stage reached outside its owner — end the UoW through "
        "open_uow / the UoW contract, or call _dispatch_outbox_after_commit "
        "(dispatch=False for a test-owned transaction); see "
        "unit_of_work.COMMIT_PIPELINE: " + ", ".join(offenders)
    )


def test_only_the_uow_contract_commits_an_async_session() -> None:
    offenders = [
        f"{path.relative_to(SRC).as_posix()}:{lineno}"
        for path in _runtime_sources()
        if path.relative_to(SRC).as_posix() != COMMIT_OWNER
        for lineno, line in enumerate(path.read_text().splitlines(), 1)
        if AWAITED_COMMIT.search(line)
    ]
    assert offenders == [], (
        "an async COMMIT outside the UnitOfWorkContract skips the commit "
        "pipeline (before_commit checks, outbox dispatch, on_commit): "
        + ", ".join(offenders)
    )


def test_the_pipeline_is_documented_in_order() -> None:
    from sandloom.core.unit_of_work import COMMIT_PIPELINE

    assert COMMIT_PIPELINE == (
        "before_commit",
        "COMMIT",
        "outbox dispatch",
        "on_commit",
    )
