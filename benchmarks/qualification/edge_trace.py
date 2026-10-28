"""Optional bounded, credential-free origin trace for this diagnostic fixture."""

from __future__ import annotations

import hmac
import os
import re
import time
from collections import deque
from urllib.parse import parse_qs

from starlette.responses import JSONResponse


class EdgeTrace:
    def __init__(self, app):
        self.app = app
        self.records = deque(maxlen=20000)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        if scope["path"] == "/_qualification/edge-trace":
            token = os.environ.get("INTERNAL_API_TOKEN", "").encode()
            supplied = headers.get(b"x-internal-token", b"")
            if not token or not hmac.compare_digest(token, supplied):
                return await JSONResponse({}, status_code=404)(scope, receive, send)
            ray = parse_qs(scope.get("query_string", b"").decode()).get("ray", [""])[0]
            hits = [dict(r) for r in self.records if r["ray"] == ray]
            return await JSONResponse({"worker": os.getpid(), "hits": hits})(
                scope, receive, send
            )
        ray = headers.get(b"cf-ray", b"").decode("ascii", "replace").split("-", 1)[0]
        if not re.fullmatch(r"[a-fA-F0-9]{16,32}", ray):
            return await self.app(scope, receive, send)
        record = {
            "ray": ray,
            "started_epoch": time.time(),
            "path": scope["path"],
            "status": None,
            "completed": False,
        }
        self.records.append(record)

        async def traced_send(message):
            if message["type"] == "http.response.start":
                record["status"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, traced_send)
            record["completed"] = True
        except BaseException as error:
            record["error_type"] = type(error).__name__
            raise
        finally:
            record["finished_epoch"] = time.time()
