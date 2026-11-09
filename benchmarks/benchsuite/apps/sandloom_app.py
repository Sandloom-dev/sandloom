"""Sandloom: the stand's composition root, loading only the ``benchsuite.bench`` module.

The runner sets ``SANDLOOM_MODULES=["benchsuite.bench"]`` and ``ENVIRONMENT=prod``
(production defaults: Redis event bus, outbox relay on, strict event schemas,
JSON logs, revocation fail-closed), so this measures Sandloom as it is deployed —
every middleware, the request Unit of Work, the real auth guard and the
transactional outbox included.

The standard root (``sandloom_standard.main``) assigns the protected claims to
identity, tenancy and billing, none of which this composition loads, so it
would trust no token. This root assigns ``tenant_ids`` and
``subscription_status`` to the ``bench`` module
(:data:`benchsuite.bench.claims.CLAIM_AUTHORITIES`); the stand's tokens are
minted through the real mint (``benchsuite.claims_mint``).
"""

import os

from benchsuite.bench.claims import CLAIM_AUTHORITIES
from sandloom.sdk.app import create_app

app = create_app(claim_authorities=CLAIM_AUTHORITIES)

if os.environ.get("BENCH_EDGE_TRACE") == "1":
    from qualification.edge_trace import EdgeTrace

    app = EdgeTrace(app)

__all__ = ["app"]
