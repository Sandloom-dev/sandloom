# ruff: noqa: F811 - the shared fixtures are imported and requested by name
"""Hot-path stand (``ladder/hot_paths.py``): the routes its rungs measure.

* ``/s5bud`` answers exactly like ``/s5`` (own row, cross-tenant 404, 401
  without a token); revocation budgets are gone (ADR-0009): both routes read
  the subject-state record live, one ``GET`` each;
* ``/dw/7`` (W3 as class F) commits its rows once per ``Idempotency-Key`` and
  replays the response for a retry (nothing written twice);
* every rung of the suite is registered in ``ladder.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from test_s5_secure_read import (  # noqa: F401 - fixtures
    ROLE,
    S5,
    A,
    B,
    admin,
    headers,
    merged_tx,
    pg_proxy,
    s5,
)
from test_write_stand import W, w  # noqa: F401 - fixtures

from benchsuite import common

#: the S5 stack (FORCEd RLS, app role, counting proxy) of the request variant
on_s5 = pytest.mark.parametrize("s5", ["uow"], indirect=True)

LADDER = Path(__file__).resolve().parents[1] / "ladder"
A_ID = 1
B_ID = common.S5_ROWS_PER_TENANT + 1


@pytest.mark.asyncio
@on_s5
async def test_budgeted_route_serves_like_the_strict_one(s5: S5) -> None:
    own = await s5.get(f"/s5bud/{A_ID}", headers=headers(A))
    assert own.status_code == 200, own.text
    assert own.json() == (await s5.get(f"/s5/{A_ID}", headers=headers(A))).json()
    assert (await s5.get(f"/s5bud/{B_ID}", headers=headers(A))).status_code == 404
    assert (await s5.get(f"/s5bud/{A_ID}")).status_code == 401
    assert (await s5.get(f"/s5bud/{A_ID}", headers=headers(B))).status_code == 404


@pytest.mark.asyncio
@on_s5
async def test_every_authenticated_read_reads_one_subject_record_get(
    s5: S5, sandloom_core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = await sandloom_core.cache().raw_client()
    sent: list[str] = []
    original = client.execute_command

    async def counting(*args: Any, **kwargs: Any) -> Any:
        sent.append(str(args[0]).upper())
        return await original(*args, **kwargs)

    monkeypatch.setattr(client, "execute_command", counting)
    h = headers(A)
    for path in (f"/s5bud/{A_ID}", f"/s5/{A_ID}"):
        assert (await s5.get(path, headers=h)).status_code == 200
        sent.clear()
        assert (await s5.get(path, headers=h)).status_code == 200
        # no local copy, no budget: one live GET of the record (G-REVOKE-21)
        assert sent == ["GET"], (path, sent)


@pytest.mark.asyncio
@on_s5
async def test_class_f_write_replays_a_retry(w: W) -> None:
    key = f"hp-{uuid.uuid4().hex}"
    h = {**headers(A), "Idempotency-Key": key}
    before = await w.counts()
    first = await w.post("/dw/7", {"name": "w7", "value": 7}, headers=h)
    assert first.status_code == 201, first.text
    after_first = await w.counts()
    retry = await w.post("/dw/7", {"name": "w7", "value": 7}, headers=h)
    assert retry.status_code == 201, retry.text
    assert retry.json() == first.json()
    assert retry.headers.get("Idempotent-Replay") == "true"
    after = await w.counts()
    assert after == after_first  # replay creates neither domain nor automatic audit
    assert after["bench_tenant_writes"] == before["bench_tenant_writes"] + 1
    assert after["outbox_messages"] == before["outbox_messages"] + 1
    assert after["audit_log"] == before["audit_log"] + 2
    actions = await w.s5.admin.fetch("SELECT action FROM audit_log ORDER BY action")
    assert [row["action"] for row in actions] == [
        "bench.write.created",
        "sandloom.sensitive_write",
    ]


@pytest.mark.asyncio
@on_s5
async def test_class_f_write_rejects_another_body_under_the_same_key(w: W) -> None:
    h = {**headers(A), "Idempotency-Key": f"hp-{uuid.uuid4().hex}"}
    assert (
        await w.post("/dw/7", {"name": "a", "value": 1}, headers=h)
    ).status_code == 201
    r = await w.post("/dw/7", {"name": "b", "value": 2}, headers=h)
    assert r.status_code == 422, r.text


def test_every_suite_rung_is_registered() -> None:
    sys.path.insert(0, str(LADDER))
    try:
        import hot_paths
    finally:
        sys.path.remove(str(LADDER))
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
    assert set(hot_paths.OWN) <= listed
    assert set(hot_paths.SUITE) <= listed
