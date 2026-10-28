"""Request-ingress primitives: the client IP behind trusted proxies, rate limits.

Read by the HTTP pipeline, the auth guards and the idempotency guard alike, so
they sit below all three (ADR-0007). ``sandloom.core.di`` re-exports
``get_client_ip`` and ``rate_limiter``.
"""
