"""The shipped Gunicorn configuration, with per-request access logging disabled."""

from sandloom.core.cli.gunicorn_conf import *  # noqa: F403 -- inherit Gunicorn settings/hooks

accesslog = None
