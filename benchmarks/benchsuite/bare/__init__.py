"""Shared building blocks of the bare (non-SANDLOOM) implementations.

Both bare FastAPI and bare Litestar use exactly this auth check and this
SQLAlchemy setup, so a difference between them is the HTTP layer only.
"""
