"""The Redis harness half loads, and a session starts, without the ``redis`` client.

The kernel's own twin (``src/sandloom/core/tests/test_testing_redis_optional.py``)
runs where this package is not installed; this one runs with the package
installed and the client hidden, as an application with the package and no
client would.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sandloom.core.testing import workers

_SCRIPT = """
import sys
from pathlib import Path
from types import SimpleNamespace

sys.modules["redis"] = None  # an `import redis` now raises ImportError

import sandloom.core.testing.plugin  # noqa: F401
from sandloom.core.testing import families, session_checks

config = SimpleNamespace(
    rootpath=Path(sys.argv[1]),
    invocation_params=SimpleNamespace(args=()),
    option=SimpleNamespace(numprocesses=None),
)
# The session-start path of an app with no Redis URL in its test environment.
session_checks.check_run_resources(config)
assert session_checks.cap_auto_workers(config, 1) == 1
assert session_checks.cap_auto_workers(config, 4) == 4

# Without the client the Redis family has nothing to do, whatever is installed.
from sandloom_redis.testing.family import FAMILY

assert FAMILY.rewrite_worker({"CACHE_URL": "redis://h:6/1"}, {}, "gw0", 0) == {}
assert FAMILY.cap_workers({"CACHE_URL": "redis://h:6/1"}, 4) == 4

# Only a Redis operation needs the client, and it says so.
from sandloom_redis.testing import resources

try:
    resources.scan_redis("redis://127.0.0.1:1/0")
except ImportError:
    pass
else:
    raise SystemExit("scan_redis ran without redis")
print("ok")
"""


def test_session_start_runs_without_the_redis_client(tmp_path: Path) -> None:
    # A run with no database or Redis URL: neither the shell's nor the ones the
    # plugin seeded into this process from .env.test.
    drop = set(workers.OVERRIDABLE_VARS)
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in drop and not k.startswith(workers.OVERRIDE_PREFIX)
    }
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT, str(tmp_path)],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
