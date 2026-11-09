"""A consumer module of this example: kernel atoms only.

It imports no other module (only ``sandloom.sdk``); what its gates read comes
from the token (acme_idp, tenancy, billing claims) or the live contracts
(``tenancy.membership.v1``, billing's subscription fact).
"""

from sandloom.sdk import define_module

register = define_module(__name__, version="0.1.0")
