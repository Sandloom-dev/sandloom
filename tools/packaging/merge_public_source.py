#!/usr/bin/env python3
"""Git merge driver for ``tools/packaging/public-source.json``.

Parallel branches each classify their new files into the same policy file, so
two branches adding files to one directory put lines beside each other and a
line merge stops on a conflict (11 of the 25 conflicts of the 14-15 Nov wave).
The policy is sets of paths, not text: this driver merges it three-way as data.

* ``include`` groups are matched by ``reason``. A path the other branch added
  goes beside its nearest sibling in the group (the classify rule of
  ``tools/agent_registry.py``); a path it removed is removed. A group only one
  side added is kept; a group one side removed goes if the other side left it
  as it was.
* Mappings (``exclude_files``, ``exclude_trees``, ``symlinks``) and every other
  top-level key merge per key: a key changed on one side takes that change.
* Both sides changing the same key or group differently, a path listed twice
  after the merge, or a side that is not valid JSON: the driver falls back to
  ``git merge-file``, so the file gets ordinary conflict markers and git
  reports it as conflicted.

Wired through ``.gitattributes`` (``merge=public-source``) and the local git
config, which ``--install`` writes once per clone (worktrees share it)::

    python3 tools/packaging/merge_public_source.py --install

Git then calls ``merge_public_source.py %O %A %B`` (``git merge-file`` where the
script is missing) and keeps the result in %A.
Without the config, git falls back to its line merge. Standard library only.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

DRIVER = "public-source"
# A branch cut before this script existed (or a machine without python3) still
# gets git's line merge with markers, never a silent copy of ours.
SCRIPT = "tools/packaging/merge_public_source.py"
COMMAND = (
    f"if [ -f {SCRIPT} ] && command -v python3 >/dev/null; "
    f"then python3 {SCRIPT} %O %A %B; "
    "else git merge-file -L ours -L base -L theirs %A %O %B; fi"
)


class Conflict(Exception):
    """Both sides changed the same part of the policy differently."""


def _common(a: str, b: str) -> tuple[int, int]:
    """Shared leading path parts, then shared leading characters."""
    pa, pb = a.split("/"), b.split("/")
    parts = 0
    for x, y in zip(pa[:-1], pb[:-1], strict=False):
        if x != y:
            break
        parts += 1
    chars = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        chars += 1
    return parts, chars


def _insert(paths: list[str], path: str) -> None:
    """Put ``path`` beside its nearest sibling, on the side its name sorts to."""
    if not paths:
        paths.append(path)
        return
    i = max(range(len(paths)), key=lambda k: _common(path, paths[k]))
    paths.insert(i + 1 if path > paths[i] else i, path)


def _merge_paths(base: list[str], ours: list[str], theirs: list[str]) -> list[str]:
    added = [p for p in theirs if p not in base]
    removed = set(base) - set(theirs)
    out = [p for p in ours if p not in removed]
    for p in added:
        if p not in out:
            _insert(out, p)
    return out


def _merge_include(
    base: list[dict], ours: list[dict], theirs: list[dict]
) -> list[dict]:
    by = {g["reason"]: g for g in base}
    mine = {g["reason"]: g for g in ours}
    other = {g["reason"]: g for g in theirs}
    out: list[dict] = []
    for g in ours:
        if g["reason"] in other:
            out.append(_merge_group(by.get(g["reason"]), g, other[g["reason"]]))
        elif _kept(by.get(g["reason"]), g, "there"):
            out.append(copy.deepcopy(g))
    out.extend(
        copy.deepcopy(g)
        for g in theirs
        if g["reason"] not in mine and _kept(by.get(g["reason"]), g, "here")
    )
    return out


def _merge_group(base: dict | None, ours: dict, theirs: dict) -> dict:
    merged = copy.deepcopy(ours)
    old = base["paths"] if base else []
    merged["paths"] = _merge_paths(old, ours["paths"], theirs["paths"])
    for key in set(ours) | set(theirs):  # any field besides the paths
        if key not in ("reason", "paths"):
            _set(merged, key, base, ours, theirs)
    return merged


def _kept(base: dict | None, group: dict, removed_where: str) -> bool:
    """A group only one side has: new there (keep), or removed on the other side."""
    if base is None:
        return True
    if group != base:
        raise Conflict(
            f"include group {group['reason']!r}: removed {removed_where},"
            " changed on the other side"
        )
    return False


_MISSING = object()


def _set(out: dict, key: str, base: Any, ours: Any, theirs: Any) -> None:
    """Three-way merge of ``key``: the side that changed it wins."""
    b = base.get(key, _MISSING) if isinstance(base, dict) else _MISSING
    o = ours.get(key, _MISSING)
    t = theirs.get(key, _MISSING)
    if o == t or t == b:
        value = o
    elif o == b:
        value = t
    else:
        raise Conflict(f"{key!r} changed on both sides")
    if value is _MISSING:
        out.pop(key, None)
    else:
        out[key] = copy.deepcopy(value)


def _merge_mapping(base: dict, ours: dict, theirs: dict) -> dict:
    out = copy.deepcopy(ours)
    for key in list(dict.fromkeys([*ours, *theirs, *base])):
        _set(out, key, base, ours, theirs)
    if list(ours) == sorted(ours):  # keep a sorted mapping sorted
        out = dict(sorted(out.items()))
    return out


def merge(base: dict, ours: dict, theirs: dict) -> dict:
    """The merged policy; raises Conflict where both sides disagree."""
    out: dict = {}
    for key in dict.fromkeys([*ours, *theirs, *base]):
        b, o, t = base.get(key), ours.get(key), theirs.get(key)
        if key == "include" and isinstance(o, list) and isinstance(t, list):
            out[key] = _merge_include(b or [], o, t)
        elif all(isinstance(x, dict) for x in (o, t)) and isinstance(
            b, (dict, type(None))
        ):
            out[key] = _merge_mapping(b or {}, o, t)
        else:
            _set(out, key, base, ours, theirs)
    _no_duplicates(out)
    return out


def _no_duplicates(policy: dict) -> None:
    """Each side was valid alone; together they may list one path twice."""
    seen: dict[str, str] = {}
    for group in policy.get("include", []):
        for path in group["paths"]:
            if path in seen:
                raise Conflict(f"{path} in two include groups after the merge")
            seen[path] = group["reason"]
    for path in policy.get("exclude_files", {}):
        if path in seen:
            raise Conflict(f"{path} both included and excluded after the merge")


def _line_merge(base: Path, ours: Path, theirs: Path) -> int:
    """Git's own line merge into ``ours``, conflict markers included.

    A conflict the driver cannot settle must look like any other conflict: a
    file with markers, not a plain copy of ours that loses their additions to
    a careless ``git add``.
    """
    argv = ["git", "merge-file", "-L", "ours", "-L", "base", "-L", "theirs"]
    done = subprocess.run([*argv, str(ours), str(base), str(theirs)], check=False)  # noqa: S603 - fixed git argv
    return done.returncode or 1


def install() -> None:
    for name, value in (
        ("name", "public-source.json three-way merge"),
        ("driver", COMMAND),
    ):
        argv = ["git", "config", f"merge.{DRIVER}.{name}", value]
        subprocess.run(argv, check=True)  # noqa: S603 - fixed git argv
    print(f"merge.{DRIVER}.driver = {COMMAND}")


def main(argv: list[str]) -> int:
    if argv == ["--install"]:
        install()
        return 0
    if len(argv) != 3:
        print(
            "usage: merge_public_source.py BASE OURS THEIRS | --install",
            file=sys.stderr,
        )
        return 2
    base_p, ours_p, theirs_p = (Path(a) for a in argv)
    try:
        base, ours, theirs = (
            json.loads(p.read_text(encoding="utf-8"))
            for p in (base_p, ours_p, theirs_p)
        )
        merged = merge(base, ours, theirs)
    except (ValueError, Conflict, KeyError, TypeError) as exc:
        print(
            f"public-source.json: not merged as data ({exc}); line merge with markers",
            file=sys.stderr,
        )
        return _line_merge(base_p, ours_p, theirs_p)
    ours_p.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
