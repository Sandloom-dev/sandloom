"""The prototype's composition root (``oidcapp.roots.mono:app``).

One root for every process of every topology: ``SANDLOOM_MODULES`` picks the
modules a process loads, and the map stays whole (a process that loads no
issuer checks none of it; the issuing one needs every authority loaded or
bound, SANDLOOM-B035).
"""

from sandloom.sdk.app import create_app

from . import CLAIM_AUTHORITIES

app = create_app(claim_authorities=CLAIM_AUTHORITIES)
core = app.state.core
