"""A consumer module: gates on rights, the platform gate, the tenant and the
inventory fact. It imports no other fixture module (only sandloom.sdk)."""

from sandloom.sdk import define_module

register = define_module(__name__, version="0.1.0")
