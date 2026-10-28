"""Informational observed effects in the route snapshot (ADR-0009 item 14,
G-ROUTE-05): the route runtime hands the effects a request ran to the test
harness's observer; ``--write`` attaches them to the snapshot; the check
ignores them, so they never gate a build or a request."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import sandloom.core.route_snapshots as snapshots
from sandloom.core import di
from sandloom.core.http.route_runtime import install_route_runtime
from sandloom.core.unit_of_work import risk


@pytest.mark.asyncio
async def test_the_runtime_reports_a_requests_effects_to_the_observer() -> None:
    app = FastAPI()

    @app.post("/pay", dependencies=[di.public(reason="probe")])
    async def pay() -> dict[str, bool]:
        risk.note_effect("money", "ledger:probe", deduped=True)
        return {"ok": True}

    @app.get("/plain", dependencies=[di.public(reason="probe")])
    async def plain() -> dict[str, bool]:
        return {"ok": True}

    install_route_runtime(app.routes, core=None)
    seen: list[tuple[str, str, dict[str, set[str]]]] = []
    risk.set_effect_observer(lambda m, p, r: seen.append((m, p, r)))
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            assert (await c.post("/pay")).status_code == 200
            assert (await c.get("/plain")).status_code == 200
        risk.set_effect_observer(lambda *_: (_ for _ in ()).throw(RuntimeError()))
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            # a failing observer never fails the request (informational)
            assert (await c.post("/pay")).status_code == 200
    finally:
        risk.set_effect_observer(None)
    assert seen == [("POST", "/pay", {"money": {"ledger:probe"}})]


def test_observed_effects_are_written_but_never_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(snapshots, "module_package_dir", lambda _m: tmp_path)
    snapshots.record_observed("m", {("POST", "/pay"): {"money": ["ledger:a"]}})
    snapshots.record_observed("m", {("POST", "/pay"): {"money": ["ledger:b"]}})
    observed = snapshots.load_observed("m")
    assert observed == {("POST", "/pay"): {"money": ["ledger:a", "ledger:b"]}}
    doc: dict[str, Any] = {
        "format": snapshots.SNAPSHOT_FORMAT,
        "module": "m",
        "routes": [
            {"method": "POST", "path": "/pay", "auth": "user", "policy": {}},
            {"method": "GET", "path": "/x", "auth": "user", "policy": {}},
        ],
    }
    written = snapshots.with_observed(doc, observed)
    pay = written["routes"][0]
    assert pay[snapshots.OBSERVED_FIELD] == {"money": ["ledger:a", "ledger:b"]}
    assert snapshots.OBSERVED_FIELD not in written["routes"][1]
    text = snapshots.render_snapshot(written)
    # the check ignores the informational field in either direction
    assert snapshots._matches_ignoring_observed(text, doc)
    assert snapshots._matches_ignoring_observed(snapshots.render_snapshot(doc), written)
    # ... but a real policy change still fails
    changed = json.loads(text)
    changed["routes"][0]["auth"] = "public"
    assert not snapshots._matches_ignoring_observed(json.dumps(changed), doc)


def test_the_harness_records_per_module_and_sandloom_test_opts_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from sandloom.core.cli.run_tests import plan
    from sandloom.core.testing import observed

    monkeypatch.setattr(snapshots, "module_package_dir", lambda _m: tmp_path)
    # this worker's own observations (``sandloom test`` observes every test) stay
    # out of this test's flush, and the observer is restored after it
    monkeypatch.setattr(observed, "_seen", {})
    monkeypatch.setattr(observed, "_modules", {})
    monkeypatch.setattr(observed, "_apps", set())
    monkeypatch.setattr(risk, "effect_observer", risk.effect_observer)
    route = SimpleNamespace(path="/pay", methods={"POST"}, sandloom_module="notes")
    kernel_route = SimpleNamespace(path="/health", methods={"GET"})
    observed.observe(SimpleNamespace(routes=[route, kernel_route]))
    try:
        assert risk.effect_observer is not None
        risk.effect_observer("POST", "/pay", {"money": {"ledger:a"}})
        risk.effect_observer("GET", "/health", {"security": {"x"}})  # kernel
    finally:
        written = observed.flush()
    assert risk.effect_observer is None
    assert written == [tmp_path / snapshots.OBSERVED_FILENAME]
    assert snapshots.load_observed("notes") == {
        ("POST", "/pay"): {"money": ["ledger:a"]}
    }

    steps = plan(tmp_path, None, full=False, gates=False, workers="2")
    assert steps[-1].env.get(observed.OBSERVE_ENV) == "1"
    ci_steps = plan(tmp_path, None, full=False, gates=False, workers="2", ci=True)
    assert observed.OBSERVE_ENV not in ci_steps[-1].env
