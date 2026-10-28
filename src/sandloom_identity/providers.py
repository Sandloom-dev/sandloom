"""SSO provider identifiers shared by identity schemas and models."""

from __future__ import annotations

from enum import Enum


class Provider(str, Enum):
    """External identity providers."""

    GOOGLE = "google"
    MICROSOFT = "microsoft"
    GITHUB = "github"
    LINKEDIN = "linkedin"
    TELEGRAM = "telegram"
