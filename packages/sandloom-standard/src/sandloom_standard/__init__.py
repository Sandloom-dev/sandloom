"""The standard Sandloom composition (the ``sandloom-standard`` distribution).

``sandloom_standard.main`` is the composition root of the standard deployment
(ADR-0012 decision 3). Importing this package builds nothing; importing
``sandloom_standard.main`` builds the app. It registers no module: it composes
the module distributions installed beside it.
"""
