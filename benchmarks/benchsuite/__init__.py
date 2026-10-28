"""Comparative HTTP benchmark suite: Sandloom vs bare FastAPI vs bare Litestar.

Every implementation serves the same four scenarios with identical semantics
(see ``benchsuite.common`` and ``benchmarks/README.md``); ``benchsuite.run``
drives them under a pinned load generator and emits JSON + Markdown.
"""
