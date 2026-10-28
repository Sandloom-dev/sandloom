"""Level 1 of the public API (spec §3): the declarative forms.

``module_router``, typed events and ``on``, typed contracts
(``contract`` / ``command`` / ``Contract`` / ``ContractRef`` /
``OptionalContract``), projections and the declarative testing kit. L1 is
built only from L2: this package imports no kernel module
(``sandloom.core``, ``sandloom.shared.authz``, ``sandloom.shared.guards``), only
``sandloom.sdk._prim`` names that a public facade exports (import-linter
contract ``sdk-decl-l1``, ``test_l1_uses_only_public_l2``).

At hand-off H0 these are typed stubs frozen to the §2.3 signatures; WS-B
implements them at H2. Private: import the facades.
"""
