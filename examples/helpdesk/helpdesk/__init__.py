"""helpdesk — a Sandloom application package."""

__version__ = "0.1.0"

#: The drivers this app runs on, ``{capability: {instance: {"driver": ...}}}``:
#: the kernel selects none, so the app does (``create_app(default_infra=...)``,
#: and ``sandloom worker check`` reads it here). ``INFRA`` overrides it per
#: instance. ``memory`` is never selected for you: development names it in
#: ``.env`` (``EVENT_BUS_BACKEND=memory``), production uses the Streams bus.
DEFAULT_INFRA = {
    "database": {"default": {"driver": "sqlalchemy"}},
    "cache": {"default": {"driver": "redis"}},
    "eventbus": {"default": {"driver": "streams"}},
    "broadcast": {"default": {"driver": "redis"}},
    "taskqueue": {"default": {"driver": "celery"}},
    "atomic_store": {"security": {"driver": "redis"}},
    "lease": {"beat": {"driver": "redis"}},
}
