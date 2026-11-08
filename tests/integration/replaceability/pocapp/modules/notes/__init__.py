"""B: tenant data. A TenantScoped table under RLS; module_router binds the tenant."""

from sandloom.sdk import define_module

register = define_module(__name__, version="0.1.0")
