"""The test Redis url of this process (the xdist worker's own database index).

The harness gives each worker its own Redis database and writes the url on the
``cache.default`` instance of ``INFRA`` (``CACHE_URL`` is a removed setting, so
``INFRA`` is the only source).
"""

from __future__ import annotations

import json
import os


def worker_redis_url() -> str:
    """The url of ``cache.default`` in this process's ``INFRA``; refuses when
    it names none."""
    infra = json.loads(os.environ.get("INFRA") or "{}")
    url = ((infra.get("cache") or {}).get("default") or {}).get("url")
    if url:
        return str(url)
    raise RuntimeError("no test Redis: INFRA names no cache.default url")
