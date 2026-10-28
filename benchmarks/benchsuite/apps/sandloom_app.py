"""Sandloom: the platform composition root, loading only the ``benchsuite.bench`` module.

The runner sets ``SANDLOOM_MODULES=["benchsuite.bench"]`` and ``ENVIRONMENT=prod``
(production defaults: Redis event bus, outbox relay on, strict event schemas,
JSON logs, revocation fail-closed), so this measures Sandloom as it is deployed —
every middleware, the request Unit of Work, the real auth guard and the
transactional outbox included. ``sandloom.main`` already builds the app at import.
"""

import os

from sandloom.main import app

if os.environ.get("BENCH_EDGE_TRACE") == "1":
    from qualification.edge_trace import EdgeTrace

    app = EdgeTrace(app)

__all__ = ["app"]
