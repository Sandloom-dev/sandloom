"""Bench ORM models on Sandloom's declarative base (tables from setup_db)."""

from sqlalchemy import BigInteger, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.core.orm import Base


class BenchItem(Base):
    __tablename__ = "bench_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False)


class BenchWrite(Base):
    __tablename__ = "bench_writes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False)
