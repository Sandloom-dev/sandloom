"""The activity module: the organization's activity feed.

It records what happens in other modules from their events (today: sent
invitations, ``invites.invitation.sent.v1``). Another team owns it and ships
it on its own schedule.
"""

from sandloom.sdk import define_module

register = define_module(__name__, version="0.1.0")
