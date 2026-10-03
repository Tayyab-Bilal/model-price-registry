from datetime import UTC, datetime
from pathlib import Path

import pytest

from model_price_registry.db import connect
from model_price_registry.registry import Registry
from model_price_registry.seed import seed

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def reg(clock) -> Registry:
    conn = connect()
    seed(conn)
    return Registry(conn, clock=clock)


def count(reg: Registry, table: str, where: str = "1") -> int:
    return reg.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]
