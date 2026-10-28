"""Offline admin manifest compiler; no ORM, lifespan or production discovery."""

from .compiler import compile_app, compile_resources

__all__ = ["compile_app", "compile_resources"]
