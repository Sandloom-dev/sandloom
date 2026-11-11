"""PoC kb-poc-authz, PA-3 stand: the decisive route over parameterized facts.

Never production. The consumer-side typed wrappers stand in for what PA-3's
generator would write (``Entitled`` with ``params=`` over ``pa3.facts.v1``).
"""

from sandloom.sdk import define_module

register = define_module(__name__, version="0.1.0")
