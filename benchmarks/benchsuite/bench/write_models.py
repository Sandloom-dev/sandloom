"""Write-path stand models (DIAGNOSTIC ONLY): tenant-owned rows under RLS.

Imported only with ``BENCH_WRITE_DIAG=1`` (and ``BENCH_S5=1``): like the S5
model, a ``TenantScoped`` model switches the ORM tenant guard to its
per-statement verdict. Tables, RLS and grants come from
``benchsuite.setup_db.write_statements``.
"""

from sqlalchemy import BigInteger, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.core.orm import Base, TenantScoped


class BenchTenantWrite(TenantScoped, Base):
    """W1-W3: one INSERTed row per request."""

    __tablename__ = "bench_tenant_writes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False)


class BenchTenantCounter(TenantScoped, Base):
    """W4: seeded rows UPDATEd by primary key (``value`` += 1)."""

    __tablename__ = "bench_tenant_counters"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    value: Mapped[int] = mapped_column(Integer, nullable=False)


class BenchTenantOrder(TenantScoped, Base):
    """W5/W6: the header row of the two-table business transaction."""

    __tablename__ = "bench_tenant_orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False)


class BenchTenantOrderLine(TenantScoped, Base):
    """W5/W6: the line row (same module, same database: a real FK is fine)."""

    __tablename__ = "bench_tenant_order_lines"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("bench_tenant_orders.id"), nullable=False
    )
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)


__all__ = [
    "BenchTenantCounter",
    "BenchTenantOrder",
    "BenchTenantOrderLine",
    "BenchTenantWrite",
]
