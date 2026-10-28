"""Stub for Celery."""

from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

class Task:
    def s(self, *args: Any, **kwargs: Any) -> Any: ...

class Celery:
    def __init__(self, *args: Any, **kwargs: Any) -> None: ...
    def task(self, func: F) -> F: ...
    def add_periodic_task(self, *args: Any, **kwargs: Any) -> None: ...
