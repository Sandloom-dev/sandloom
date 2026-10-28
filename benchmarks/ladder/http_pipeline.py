"""Compare the standalone and fused HTTP layers without network/DB noise.

Run from the repository root: .venv/bin/python benchmarks/ladder/http_pipeline.py.
This is a sequential CPU microbenchmark, not an external RPS qualification.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import sys
import time
from typing import Any

from fastapi import FastAPI

from sandloom.core.http.middlewares import (
    SandloomPipelineMiddleware,
    ProcessTimeMiddleware,
    RequestIDMiddleware,
    SecurityHeadersMiddleware,
)
from sandloom.core.idempotency import IdempotencyCaptureMiddleware
from sandloom.core.config.settings import settings

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts")
)
import ci_lock  # noqa: E402  (the lock local CI holds; tools/ci/ci_lock.py)


def legacy(app: Any) -> Any:
    return RequestIDMiddleware(
        SecurityHeadersMiddleware(
            IdempotencyCaptureMiddleware(ProcessTimeMiddleware(app))
        )
    )


async def endpoint(scope: Any, receive: Any, send: Any) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", b"11"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": b'{"ok":true}'})


async def receive() -> Any:
    return {"type": "http.request", "body": b"", "more_body": False}


async def send(message: Any) -> None:
    pass


def scope(incoming_id: bool) -> dict[str, Any]:
    return {
        "type": "http",
        "http_version": "1.1",
        "asgi": {"version": "3.0"},
        "scheme": "http",
        "server": ("127.0.0.1", 8000),
        "client": ("127.0.0.1", 1234),
        "method": "GET",
        "path": "/s1",
        "raw_path": b"/s1",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"x-request-id", b"benchmark-id")] if incoming_id else [],
    }


async def measure(app: Any, count: int, incoming_id: bool) -> dict[str, float]:
    cpu = time.process_time_ns()
    wall = time.perf_counter_ns()
    for _ in range(count):
        await app(scope(incoming_id), receive, send)
    return {
        "cpu_us": (time.process_time_ns() - cpu) / count / 1000,
        "wall_us": (time.perf_counter_ns() - wall) / count / 1000,
    }


async def validate(app: Any, incoming_id: bool) -> None:
    messages = []

    async def collect(message: Any) -> None:
        messages.append(message)

    await app(scope(incoming_id), receive, collect)
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 200
    assert b"".join(m.get("body", b"") for m in messages[1:]) == b'{"ok":true}'
    headers = dict(messages[0]["headers"])
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert (
        headers[b"strict-transport-security"] == b"max-age=63072000; includeSubDomains"
    )
    assert float(headers[b"x-process-time"]) >= 0


async def main(count: int, pairs: int) -> None:
    settings.environment = "prod"
    router = FastAPI()

    @router.get("/s1")
    async def s1() -> dict[str, bool]:
        return {"ok": True}

    reports = []
    for kind, inner in (("asgi", endpoint), ("fastapi", router)):
        for incoming_id in (False, True):
            apps = {"legacy": legacy(inner), "fused": SandloomPipelineMiddleware(inner)}
            for app in apps.values():
                await validate(app, incoming_id)
                await measure(app, 2000, incoming_id)
            windows: dict[str, list[dict[str, float]]] = {name: [] for name in apps}
            for pair in range(pairs):
                order = ("legacy", "fused") if pair % 2 == 0 else ("fused", "legacy")
                for name in order:
                    windows[name].append(await measure(apps[name], count, incoming_id))
            medians = {
                name: statistics.median(v["cpu_us"] for v in values)
                for name, values in windows.items()
            }
            reports.append(
                {
                    "kind": kind,
                    "incoming_id": incoming_id,
                    "iterations_per_window": count,
                    "windows": windows,
                    "median_cpu_us": medians,
                    "cpu_reduction_percent": 100
                    * (1 - medians["fused"] / medians["legacy"]),
                }
            )
    print(
        json.dumps(
            {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "production_headers": True,
                "reports": reports,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=30_000)
    parser.add_argument("--pairs", type=int, default=6)
    args = parser.parse_args()
    if args.iterations < 1 or args.pairs < 2:
        parser.error("iterations must be positive and pairs must be at least two")
    with ci_lock.held({"tool": "benchmarks/ladder/http_pipeline.py"}) as lock:
        if lock is None:
            parser.error(
                "local CI or another CPU benchmark is running:\n"
                + ci_lock.holder_text()
            )
        asyncio.run(main(args.iterations, args.pairs))
