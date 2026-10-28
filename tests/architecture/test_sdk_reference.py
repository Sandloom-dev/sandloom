"""``sandloom docs`` and the generated reference blocks (spec §2.5; C-SDK-01/02).

Every public ``sandloom.sdk`` module starts with a current reference block (its
names by tier and its doc page); ``sandloom docs <name>`` answers a name's home,
tier, signature and documentation without a kernel read. A stale block fails
like a changed surface (SANDLOOM-T008).
"""

from __future__ import annotations

import pytest

from sandloom.sdk._prim import docs


def test_every_public_module_carries_a_current_reference_block() -> None:
    assert docs.stale_blocks() == [], "run `uv run sandloom docs --write`"
    for module in docs.public_modules():
        assert docs.BEGIN in (module.__doc__ or ""), module.__name__


def test_a_block_lists_the_tiers_and_the_doc_page() -> None:
    import sandloom.sdk.events

    block = docs.reference_block(sandloom.sdk.events)
    assert "beginner: Event, on" in block
    assert "publish_event" in block
    assert "docs/reference/sdk/events.md" in block


def test_sandloom_docs_answers_a_name(capsys: pytest.CaptureFixture[str]) -> None:
    assert docs.docs_main(["module_router"]) == 0
    out = capsys.readouterr().out
    assert "sandloom.sdk.web.module_router(" in out
    assert "tier: beginner" in out
    assert "router = module_router()" in out  # the docstring example
    assert docs.docs_main(["sandloom.sdk.sagas"]) == 0
    assert "advanced:" in capsys.readouterr().out
    assert docs.docs_main(["no_such_name"]) == 1


def test_every_beginner_name_documents_an_example() -> None:
    """Spec §2.5: examples in every beginner docstring (code names)."""
    import inspect

    import sandloom.sdk

    missing = []
    for name in sandloom.sdk.__beginner__:
        obj = getattr(sandloom.sdk, name)
        if not str(getattr(obj, "__module__", "")).startswith("sandloom.sdk"):
            continue  # a kernel object re-exported as is, or a type alias
        doc = inspect.getdoc(obj) or ""
        if "::" not in doc and ">>>" not in doc and "``" not in doc:
            missing.append(name)
    assert missing == [], missing
