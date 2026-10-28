"""The invites module: members invite people from outside the product.

An invitation is a row of the organization (``TenantScoped``) with a link
token (only its hash is stored) and an expiry. Our mail service delivers the
link when this module announces ``invites.invitation.sent.v1``; the
``activity`` module records it in the organization's feed.
"""

from sandloom.sdk import define_module

register = define_module(__name__, version="0.1.0")
