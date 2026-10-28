"""Durable event consumers: the descriptor, the handler wrapper and the runtime.

``callbacks`` turns a registered consumer into a bus callback that applies each
delivery in its own Unit of Work (exactly-once with ``durable=True``, metrics,
lag); ``runtime`` subscribes, supervises and reports every consumer of the
application. Above the Unit of Work and the event bus (ADR-0007).
"""
