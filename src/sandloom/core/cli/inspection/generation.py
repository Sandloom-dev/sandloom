"""Generation integration kept out of cold reader imports."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from sandloom.core.cli.inspection import collect, files
from sandloom.core.cli.inspection.model import INDEX, RECEIPT, InspectionError
from sandloom.core.cli.inspection.read import Reader, parse


def publish_index(path: Path, text: str) -> None:
    files.atomic(path.parent.parent, INDEX, text)


def capture(rules: Any) -> dict[str, str] | None:
    try:
        return collect.capture(rules)
    except (InspectionError, OSError, UnicodeError):
        print("warning[SANDLOOM-T015]: inspection input basis unavailable")
        return None


def _document(app: Any, rules: Any, complete: list[Any]) -> Any:
    try:
        return collect.collect(app, rules, complete)
    except (InspectionError, OSError, UnicodeError, SyntaxError):
        print("warning[SANDLOOM-T015]: inspection metadata unavailable")
        return None


def finish(
    *,
    rules: Any,
    app: Any,
    selected: list[Any],
    modules: list[str],
    check: bool,
    before: dict[str, str] | None,
    plan_all: Callable[[], list[Any]],
    make_file: Callable[..., Any],
    apply_files: Callable[..., int],
) -> int:
    complete = plan_all() if modules else selected
    selected_paths = {item.path for item in selected}
    deferred = any(
        item.problems or item.stale
        for item in complete
        if item.path not in selected_paths
    )
    document = (
        _document(app, rules, complete) if not deferred and before is not None else None
    )
    if before is not None and before != capture(rules):
        print(
            "error[SANDLOOM-T015]: source changed during generation; "
            "retry; nothing written"
        )
        return 1
    if document is not None:
        selected.append(
            make_file(rules.root / INDEX, collect.rendered(document), "SANDLOOM-T015")
        )
    code = apply_files(
        selected, write=not check, shared_dir=rules.shared_dir, root=rules.root
    )
    if code:
        return code
    if deferred:
        print("warning[SANDLOOM-T015]: index retained; run full sandloom generate")
    elif document is None:
        if not check:
            (rules.root / RECEIPT).unlink(missing_ok=True)
        print("warning[SANDLOOM-T015]: index retained without new verification")
    elif not check:
        try:
            receipt = Reader(rules.root).receipt()
            if parse(receipt)["issues"]:
                print(
                    "warning[SANDLOOM-T015]: some interfaces are incomplete; "
                    "graph/context report their scope"
                )
            files.atomic(rules.root, RECEIPT, receipt)
        except InspectionError as exc:
            # Existing generation remains usable when inspection metadata is incomplete.
            # Withhold receipt; the cold reader diagnoses its precise missing input.
            (rules.root / RECEIPT).unlink(missing_ok=True)
            print(f"warning[SANDLOOM-T015]: inspection unverified ({exc.kind})")
    verb = "up to date" if check else "current"
    print(f"generated files {verb} ({len(selected)} file(s))")
    return 0
