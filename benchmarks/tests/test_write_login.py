"""Write / login stand (``ladder/write_login.py``): rung registration and the
``layers`` attribution mode.

* every rung of the suite is registered in ``ladder.py`` (``list``);
* the layer rules: the innermost matching frame owns the time, a sticky
  layer keeps its callees unless a sub-rule splits it;
* ``mode_layers`` attributes a synthetic op's time to the right layers and
  never reports a negative layer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

LADDER = Path(__file__).resolve().parents[1] / "ladder"


def _stand() -> Any:
    sys.path.insert(0, str(LADDER))
    try:
        import write_login
    finally:
        sys.path.remove(str(LADDER))
    return write_login


def test_every_suite_rung_is_registered() -> None:
    stand = _stand()
    env = {k: v for k, v in os.environ.items() if not k.startswith("SANDLOOM_")}
    result = subprocess.run(
        [sys.executable, str(LADDER / "ladder.py"), "list"],
        capture_output=True,
        text=True,
        env=env,
        cwd=LADDER.parent,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    listed = {line.split()[0] for line in out.splitlines() if line.strip()}
    assert set(stand.OWN) <= listed
    assert set(stand.SUITE) <= listed


def _code(filename: str, name: str) -> Any:
    ns: dict[str, Any] = {}
    exec(compile(f"def {name}():\n    return 1\n", filename, "exec"), ns)  # noqa: S102
    return ns[name].__code__


def test_layer_rules_match_files_and_names() -> None:
    stand = _stand()
    cache: dict[Any, Any] = {}
    reserve = _code("/x/sandloom/core/idempotency/__init__.py", "_dependency")
    other = _code("/x/sandloom/core/idempotency/__init__.py", "_helper")
    commit = _code("/x/sqlalchemy/ext/asyncio/session.py", "commit")
    assert stand._rule_for(reserve, cache) == ("idempotency reserve", True)
    assert stand._rule_for(other, cache) is None
    assert stand._rule_for(commit, cache) == ("COMMIT", True)
    flush = _code("/x/sqlalchemy/orm/session.py", "flush")
    redis = _code("/x/redis/asyncio/client.py", "execute_command")
    sub: dict[Any, Any] = {}
    assert (
        stand._sub_rule_for(flush, "COMMIT", sub)
        == "COMMIT > ORM flush (pending INSERT/UPDATE)"
    )
    assert stand._sub_rule_for(flush, "idempotency reserve", sub) is None
    assert (
        stand._sub_rule_for(redis, "idempotency reserve", sub)
        == "idempotency reserve > Redis client"
    )


@pytest.mark.asyncio
async def test_mode_layers_attributes_a_synthetic_op() -> None:
    stand = _stand()
    ns: dict[str, Any] = {}
    src = (
        "def _dependency():\n"
        "    t = 0\n"
        "    for i in range(20000):\n"
        "        t += i\n"
        "    return t\n"
    )
    exec(compile(src, "/x/sandloom/core/idempotency/__init__.py", "exec"), ns)  # noqa: S102
    busy = ns["_dependency"]

    async def op(_i: int) -> Any:
        return busy()

    res = await stand.mode_layers(op, SimpleNamespace(warmup=2, n=20))
    layers = res["layers_us"]
    assert layers["idempotency reserve"] > 0
    assert all(v >= 0 for v in layers.values())
    assert res["main_thread_us"] == pytest.approx(sum(layers.values()), rel=0.01)
    assert max(layers, key=layers.get) == "idempotency reserve"


def _oracle_mode() -> Any:
    qual = Path(__file__).resolve().parents[1] / "qualification"
    sys.path.insert(0, str(qual))
    try:
        import write_login_perf
    finally:
        sys.path.remove(str(qual))
    return write_login_perf


def test_oracle_override_merges_the_probe_into_the_app_service(
    tmp_path: Path,
) -> None:
    mode = _oracle_mode()
    path = mode._OverrideFile(tmp_path / "variant.yml")
    path.extra = {"command": ["gunicorn", "x:app"], "volumes": ["/h:/tail"]}
    body = {"services": {"app": {"image": "i", "volumes": ["/b:/benchmarks:ro"]}}}
    path.write_text(json.dumps(body))
    app = json.loads((tmp_path / "variant.yml").read_text())["services"]["app"]
    assert app["command"] == ["gunicorn", "x:app"]
    assert app["volumes"] == ["/b:/benchmarks:ro", "/h:/tail"]
    assert app["image"] == "i"


def test_oracle_compare_reports_medians(tmp_path: Path) -> None:
    mode = _oracle_mode()
    for name, rps in (("before", (300, 320, 310)), ("after", (330, 340, 335))):
        d = tmp_path / name
        d.mkdir()
        rows = [
            {"section": "financial-write", "goodput_rps": r, "p99_ms": 100.0}
            for r in rps
        ]
        (d / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    out = tmp_path / "cmp.md"
    mode.compare(
        SimpleNamespace(
            before=str(tmp_path / "before"), after=str(tmp_path / "after"), out=str(out)
        )
    )
    assert (
        "| financial-write | goodput_rps | 310.0 | 335.0 | +8.1% |" in out.read_text()
    )
