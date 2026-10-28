"""Declared operations of the ``bench`` module (execution-planner PROTOTYPE).

The module states WHAT it reads and in which shape; the kernel's planner picks
HOW (ORM general executor, or the optimized driver-level executor when
``PLANNER_OPTIMIZED_EXECUTOR=true``) under one shared guarantees layer.
Imported only with ``BENCH_S5=1`` (its entity is ``TenantScoped``).
"""

from sandloom.core.orm import ListOperation, ReadOperation

from .api import ItemOut  # defined before api imports the S5 routes
from .tenant_models import BenchTenantItem

#: ``bench_tenant_items`` by primary key, as the ``ItemOut`` DTO (tenant-scoped:
#: inferred from the entity; the plan always carries the tenant predicate).
get_tenant_item = ReadOperation(
    entity=BenchTenantItem,
    result=ItemOut,
    by=("id",),
    name="bench.tenant_item.get",
)

#: the tenant's items, one keyset page per call (``GET /s5list``)
list_tenant_items = ListOperation(
    entity=BenchTenantItem,
    result=ItemOut,
    order_by=("id",),
    max_limit=200,
    name="bench.tenant_items.list",
)

__all__ = ["get_tenant_item", "list_tenant_items"]
