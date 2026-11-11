"""Grants (user id -> tenant ids) and the provider-down switch, process-local."""

GRANTS: dict[str, set[str]] = {}
DOWN = {"on": False}
