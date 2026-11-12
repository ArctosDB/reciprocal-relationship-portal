import os
from pathlib import Path

import duckdb
import pytest


DB_PATH = Path(__file__).parent.parent / "data" / "arctos.duckdb"


def pytest_collection_modifyitems(config, items):
    if DB_PATH.exists():
        return
    skip = pytest.mark.skip(reason=f"{DB_PATH} not found (set ARCTOS_DB)")
    for item in items:
        if "duckdb" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def db():
    conn = duckdb.connect(str(DB_PATH), read_only=True, config={"memory_limit": "12GB"})
    yield conn
    conn.close()


@pytest.fixture
def cursor(db):
    with db.cursor() as cur:
        yield cur
