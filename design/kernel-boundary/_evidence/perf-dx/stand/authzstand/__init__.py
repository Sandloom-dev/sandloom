"""Authorization baseline stand (kb-perf-dx-baseline): one route per policy.

Never production. Loaded with ``SANDLOOM_MODULES=[..., "authzstand"]`` and this
directory's parent on ``PYTHONPATH``; the routes are in ``api/``.
"""

from sandloom.sdk import define_module

register = define_module(__name__, version="0.1.0")
