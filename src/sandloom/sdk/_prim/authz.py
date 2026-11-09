"""The account subject of ``sandloom.sdk.authz`` (spec §2.3, §7).

Every generated access atom (e.g. ``Subscribed``, ``SubscriptionActive``,
``OrgRole`` from the provider's stub) takes a required ``of=`` where it
checks an account: ``Account.user()``,
``Account.org(param=...)`` or ``Account.bound_tenant()`` (H1, WS-A).
"""

from __future__ import annotations

from sandloom.shared.authz import Account, current_context

__all__ = ["Account", "current_context"]
