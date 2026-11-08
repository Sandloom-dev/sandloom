"""The inventory's console screen: plain data, no import of the console."""

from typing import Any

RESOURCES: list[dict[str, Any]] = [
    {
        "name": "stock",
        "label": "Stock",
        "group": "Inventory",
        "icon": "package",
        "access": {"roles": ["admin"]},
        "list": {"path": "/api/v1/inventory/stock", "method": "GET"},
        "pagination": {"mode": "offset"},
        "columns": ["sku", "quantity"],
        "labels": {"sku": "SKU", "quantity": "Quantity"},
    }
]
