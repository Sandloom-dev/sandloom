"""S5 model: a tenant-owned row (ORM tenant guard + PostgreSQL RLS).

Imported only when S5 is mounted (``BENCH_S5=1``): defining a ``TenantScoped``
model changes the ORM tenant guard's hot path for EVERY statement.
"""

from sqlalchemy import BigInteger, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.core.orm import Base, TenantScoped


class BenchTenantItem(TenantScoped, Base):
    __tablename__ = "bench_tenant_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False)
