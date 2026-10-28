"""The billing-account subject of ``sandloom.sdk.authz`` (spec §2.3, §7).

``Subscribed``, ``SubscriptionActive``, ``Credits``, ``Feature`` and
``WithinLimit`` take a required ``of=``: ``Account.user()``,
``Account.org(param=...)`` or ``Account.bound_tenant()`` (H1, WS-A).
"""

from __future__ import annotations

from sandloom.shared.authz import Account, current_context

__all__ = ["Account", "current_context"]
