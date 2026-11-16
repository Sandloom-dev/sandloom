"""The fleet agents' outline: structure and one symbol of a file, with line ranges."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("outline", ROOT / "tools/outline.py")
assert _spec and _spec.loader
outline = importlib.util.module_from_spec(_spec)
sys.modules["outline"] = outline  # dataclasses resolve annotations through it
_spec.loader.exec_module(outline)

PY = '''\
"""Doc."""
import typing

T = typing.TypeVar("T")
LIMIT = 3
__all__ = ["Store"]


class Store(Base, metaclass=Meta):
    """A store."""

    @property
    def size(self) -> int:
        return 1

    async def put(self, key: str, *, ttl: int = 5) -> None:
        def helper():
            pass
        return None

    class Inner:
        pass


@decorate(1)
def top(a, b=2):
    return a
'''

MD = """\
# Title

## One
text
```python
# not a heading
```
### One.a
## Two
"""


@pytest.fixture
def py(tmp_path: Path) -> Path:
    path = tmp_path / "m.py"
    path.write_text(PY)
    return path


def test_a_python_file_lists_classes_methods_and_constants_with_ranges(
    py: Path,
) -> None:
    lines = outline.render_file(py)
    assert lines[0] == f"{py}  (27 lines)"
    body = [line.split(None, 1) for line in lines[1:]]
    assert body == [
        ["5", "LIMIT"],
        ["6", "__all__"],
        ["9-22", "class Store(Base, metaclass=Meta)"],
        ["12-14", "@property def size(self) -> int"],
        ["16-19", "async def put(self, key: str, *, ttl: int=5) -> None"],
        ["21-22", "class Inner"],
        ["25-27", "@decorate def top(a, b=2)"],
    ]


def test_a_method_prints_its_numbered_source(py: Path) -> None:
    lines = outline.render_symbol(py, "Store.put")
    assert lines[0] == f"{py}::Store.put  (lines 16-19)"
    assert lines[1].split(None, 1) == [
        "16",
        "async def put(self, key: str, *, ttl: int = 5) -> None:",
    ]
    assert len(lines) == 5


def test_a_class_prints_its_members_not_bodies(py: Path) -> None:
    lines = outline.render_symbol(py, "Store")
    assert [line.split()[0] for line in lines[1:]] == [
        "9-22",
        "12-14",
        "16-19",
        "21-22",
    ]


def test_a_bare_method_name_finds_it_and_a_wrong_one_names_the_closest(
    py: Path,
) -> None:
    assert outline.render_symbol(py, "put")[0].endswith("(lines 16-19)")
    with pytest.raises(LookupError, match="closest: Store.put"):
        outline.render_symbol(py, "Store.puts")


def test_markdown_headings_skip_fenced_code_and_end_at_the_next_peer(
    tmp_path: Path,
) -> None:
    md = tmp_path / "d.md"
    md.write_text(MD)
    lines = outline.render_file(md)[1:]
    assert [line.split(None, 1) for line in lines] == [
        ["1-9", "# Title"],
        ["3-8", "## One"],
        ["8", "### One.a"],
        ["9", "## Two"],
    ]
    # Nesting is shown by indentation after the 11-wide range column.
    assert [line[13:] for line in lines] == [
        "# Title",
        "  ## One",
        "    ### One.a",
        "  ## Two",
    ]
    section = outline.render_symbol(md, "one.a")
    assert section[0] == f"{md}::One.a  (lines 8-8)"
    assert outline.render_symbol(md, "One")[0].endswith("name one:")


def test_other_files_say_to_grep_and_long_output_is_cut(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other = tmp_path / "x.yml"
    other.write_text("a: 1\n")
    assert outline.main([str(other)]) == 2
    assert "grep -n instead" in capsys.readouterr().out
    many = tmp_path / "many.py"
    many.write_text("".join(f"def f{i}():\n    pass\n" for i in range(300)))
    assert outline.main([str(many)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == outline.MAX_LINES + 1
    assert out[-1].startswith("... 51 more lines")


def test_the_repository_parses() -> None:
    lines = outline.render_file(ROOT / "tools/outline.py")
    assert any("def render_symbol(path: Path, name: str)" in line for line in lines)


def test_any_failure_is_one_error_line_naming_the_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    good = tmp_path / "good.py"
    good.write_text("def ok():\n    pass\n")
    locked = tmp_path / "locked.py"
    locked.write_text("x = 1\n")
    locked.chmod(0)
    try:
        assert outline.main([str(locked), f"{tmp_path}::x", str(good)]) == 2
    finally:
        locked.chmod(0o644)
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith(f"error: {locked}: ")
    assert out[1] == f"error: {tmp_path}: a directory has no symbols; name a file"
    assert out[2] == f"{good}  (2 lines)"
    for exc in (MemoryError(), RecursionError("maximum recursion depth exceeded")):

        def boom(*args: object, exc: BaseException = exc, **kwargs: object) -> None:
            raise exc

        monkeypatch.setattr(outline.ast, "parse", boom)
        assert outline.main([str(good)]) == 2
        line = capsys.readouterr().out.strip()
        assert line.startswith(f"error: {good}: {type(exc).__name__}: ")


def test_a_bad_file_in_a_directory_leaves_the_others(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "a.py").write_text("def a():\n    pass\n")
    (tmp_path / "b.py").write_text("def b(:\n")
    (tmp_path / "c.py").write_text("class C:\n    pass\n")
    assert outline.main([str(tmp_path)]) == 2
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"{tmp_path / 'a.py'}  (2 lines)"
    assert out[2].startswith(f"error: {tmp_path / 'b.py'}: ")
    assert out[3] == f"{tmp_path / 'c.py'}  (2 lines)"


def test_names_defined_under_if_or_try_are_listed(tmp_path: Path) -> None:
    path = tmp_path / "guarded.py"
    path.write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    class Hint:\n"
        "        pass\n"
        "try:\n"
        "    def fast():\n"
        "        pass\n"
        "except ImportError:\n"
        "    def fast():\n"
        "        pass\n"
    )
    labels = [line.split(None, 1)[1] for line in outline.render_file(path)[1:]]
    assert labels == ["class Hint", "def fast()", "def fast()"]
