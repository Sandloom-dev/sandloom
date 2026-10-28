"""Access guard for internal/ops endpoints.

Covers ``/diagnostics/*``, ``/contracts/*``, ``/_sandloom/rpc/*`` (internal service
RPC), ``/health`` details and ``/metrics``.

Policy (fail closed):

- token configured (any environment)         -> ``X-Internal-Token`` must match;
- no token + ``INTERNAL_ENDPOINTS_OPEN_IN_DEV=true`` outside production
                                             -> open (explicit developer opt-in);
- otherwise (no token)                       -> endpoints answer 404 (disabled,
  existence hidden). ``ENVIRONMENT`` defaulting to ``dev`` therefore no longer
  opens internal RPC to anyone who can reach the port.

``/metrics`` additionally accepts the dedicated, scrape-only ``METRICS_TOKEN``
so a Prometheus scraper never holds the credential that unlocks full internal
RPC.
"""

from __future__ import annotations

import secrets
from uuid import UUID

from fastapi import Header, HTTPException, status

from sandloom.core.config.settings import settings
from sandloom.core.guard_order import authenticates

#: The subject id of a service principal (the nil UUID, like the
#: authorization subsystem's ``service_subject()``; distinguishable in audit).
SERVICE_PRINCIPAL_ID = UUID(int=0)


def _token_matches(presented: str | None, expected: str | None) -> bool:
    # Compare bytes: compare_digest(str, str) raises TypeError on non-ASCII,
    # and a header byte >= 0x80 arrives latin-1 decoded (a 500, not the 404).
    if not (expected and presented):
        return False
    return secrets.compare_digest(
        presented.encode("utf-8", "surrogatepass"),
        expected.encode("utf-8", "surrogatepass"),
    )


def _open_without_token() -> bool:
    return bool(
        not settings.is_prod
        and getattr(settings, "internal_endpoints_open_in_dev", False)
    )


def has_internal_access(x_internal_token: str | None) -> bool:
    """Non-raising variant: True when the caller may see internal details."""
    token = settings.internal_api_token
    if token:
        return _token_matches(x_internal_token, token)
    return _open_without_token()


def has_metrics_access(x_internal_token: str | None) -> bool:
    """True when the caller may scrape ``/metrics``.

    Accepts the dedicated ``METRICS_TOKEN`` or the full internal token. When
    neither is configured the internal-endpoint policy applies (closed unless
    the dev opt-in is set).
    """
    metrics_token = getattr(settings, "metrics_token", None)
    if metrics_token and _token_matches(x_internal_token, metrics_token):
        return True
    if settings.internal_api_token:
        return _token_matches(x_internal_token, settings.internal_api_token)
    if metrics_token:
        return False
    return _open_without_token()


@authenticates(kind="service")
async def require_internal_access(
    x_internal_token: str | None = Header(default=None),
) -> None:
    if not has_internal_access(x_internal_token):
        # 404, not 403: hide the existence of internal endpoints
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


@authenticates(kind="service")
async def require_metrics_access(
    x_internal_token: str | None = Header(default=None),
) -> None:
    if not has_metrics_access(x_internal_token):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
