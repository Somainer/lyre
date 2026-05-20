"""Shared pytest fixtures.

Each test gets a fresh in-memory-ish SQLite database (actually a temp file because
aiosqlite + WAL needs a real file path; in-memory connections can't share state)
plus a Repositories facade wired to it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from lyre.persistence.db import init_db
from lyre.persistence.sqlite_impl import SqliteRepositories


@pytest_asyncio.fixture
async def repos(tmp_path: Path) -> AsyncIterator[SqliteRepositories]:
    db_path = tmp_path / "lyre-test.db"
    conn = await init_db(db_path)
    try:
        yield SqliteRepositories(conn)
    finally:
        await conn.close()


@pytest.fixture
def object_store(tmp_path: Path) -> Path:
    p = tmp_path / "object_store"
    p.mkdir(parents=True, exist_ok=True)
    return p
