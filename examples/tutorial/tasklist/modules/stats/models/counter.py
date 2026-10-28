"""One counter row per tenant: how many task items it created."""

import uuid

from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, TenantScoped, new_id


# --8<-- [start:model]
class StatsItemCount(TenantScoped, Base):
    """``TenantScoped``: the row belongs to the bound tenant."""

    __tablename__ = "stats_item_counts"
    __table_args__ = (UniqueConstraint("tenant_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    items: Mapped[int] = mapped_column(default=0)


# --8<-- [end:model]
