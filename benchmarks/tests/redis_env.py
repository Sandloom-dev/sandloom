"""The test Redis url of this process (the xdist worker's own database index).

The harness gives each worker its own Redis database and writes the url on the
``cache.default`` instance of ``INFRA``; until the harness feeds ``INFRA`` from
``SANDLOOM_TEST_CACHE_URL`` alone (``idk-flat-urls-s0b``), a ``.env.test`` that
still names the flat url is the only source, which is the fallback here.
"""

from __future__ import annotations

import json
import os


def worker_redis_url() -> str:
    """The url of ``cache.default`` in this process's ``INFRA``, else the flat
    url the harness copied from ``.env.test``; refuses when neither is set."""
    infra = json.loads(os.environ.get("INFRA") or "{}")
    url = ((infra.get("cache") or {}).get("default") or {}).get("url")
    if url:
        return str(url)
    flat = os.environ.get("CACHE_URL")
    if flat:
        return flat
    raise RuntimeError(
        "no test Redis: INFRA names no cache.default url and the harness "
        "gave no flat one"
    )
