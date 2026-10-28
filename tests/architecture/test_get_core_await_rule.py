"""``sandloom check`` rule ``get-core-await``: the 0.6.0 ``get_core`` break is flagged.

``sandloom.core.di.get_core`` became ``async``. ``Depends(get_core)`` is unchanged,
but a DIRECT ``get_core(request)`` call now returns a coroutine; the rule
points at every un-awaited direct call (fix: ``await get_core(request)`` or
the sync ``core_from_request(request)``).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from sandloom.core.archcheck.analyzers import find_unawaited_get_core
from sandloom.core.archcheck.runner import RULES


def _scan(tmp_path: Path, source: str) -> list[str]:
    path = tmp_path / "mod.py"
    path.write_text(textwrap.dedent(source))
    return find_unawaited_get_core(path)


def test_rule_is_registered() -> None:
    assert "get-core-await" in RULES


@pytest.mark.parametrize(
    "source",
    [
        """
        from sandloom.core.di import get_core
        def handler(request):
            core = get_core(request)
        """,
        """
        import sandloom.core.di as di
        async def handler(request):
            return di.get_core(request=request).settings
        """,
    ],
)
def test_unawaited_direct_calls_are_flagged(tmp_path: Path, source: str) -> None:
    found = _scan(tmp_path, source)
    assert len(found) == 1 and "await get_core(request)" in found[0]


def test_awaited_calls_and_depends_are_fine(tmp_path: Path) -> None:
    assert (
        _scan(
            tmp_path,
            """
            from fastapi import Depends
            from sandloom.core.di import core_from_request, get_core
            async def a(request):
                return (await get_core(request)).settings
            async def b(core=Depends(get_core)):
                return core
            def c(request):
                return core_from_request(request)
            """,
        )
        == []
    )


async def test_core_from_request_equals_awaited_get_core() -> None:
    from types import SimpleNamespace

    from sandloom.core.di import core_from_request, get_core

    class _Facade:
        def runtime_view(self) -> str:
            return "runtime"

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(core=_Facade()))
    )
    assert core_from_request(request) == await get_core(request) == "runtime"  # type: ignore[arg-type]
