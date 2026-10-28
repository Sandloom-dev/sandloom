"""The current request id, readable by logging, tracing and audit rows.

Bound by the HTTP request pipeline (``sandloom.core.http.middlewares``, which
re-exports it) and the internal RPC server; read by the structured-log
formatter and the audit writer. It lives at the bottom of the kernel so a
low-level reader never imports the HTTP layer to reach it (ADR-0007).
"""

from __future__ import annotations

from contextvars import ContextVar

#: Current request id, available to logging/tracing without threading it around.
request_id_var: ContextVar[str | None] = ContextVar("sandloom_request_id", default=None)

__all__ = ["request_id_var"]
