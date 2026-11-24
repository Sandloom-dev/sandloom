---
title: A contract command is not atomic with your unit, so make it a saga step
impact: high
tags: contracts, sagas, consistency
---

A contract command has committed in the provider when it returns and stays applied if your unit rolls back: run it as a saga step with a compensation.

A rollback cannot call a command back. When a later step can fail, add the saga with `sandloom add saga <module> <name>`, or drive the effect from an event.

## Incorrect

```python
async def place_order(order: OrderIn) -> None:
    await stock.reserve(order.sku)  # committed by the provider now
    db().add(Order(sku=order.sku))  # if this fails, the stock stays reserved
```

## Correct

<!-- code-ok: D4 rule example, a fragment of a module file: D1 and D2 check it -->
```python
PLACE_ORDER = Saga(
    "orders.place",
    version=1,
    input=OrderIn,
    deadline=timedelta(minutes=5),
    steps=[
        step(
            "reserve",
            call=stock.reserve,
            args=lambda c: {"sku": c.input.sku},
            compensate=stock.release,  # undoes the reservation if a later step fails
        ),
    ],
)
```
