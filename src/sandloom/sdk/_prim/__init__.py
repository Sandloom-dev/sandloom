"""Level 2 of the public API (spec §3): primitives the public facades export
and the declarative level (``sandloom.sdk._decl``) is built from.

Kernel-backed names are re-exported by the facades straight from the public
kernel modules; this package holds the **new** L2 names of Public API 1.0
(spec §2.2, §2.3). At hand-off H0 they are typed stubs frozen to their §2.3
signatures that raise ``NotImplementedError`` naming the section and the
hand-off that implements them (:func:`sandloom.sdk._stub.not_yet`).
Pure data types (frozen dataclasses, protocols, type aliases) are real.

Private: modules import the facades (``sandloom.sdk.<theme>``), never this package.
"""
