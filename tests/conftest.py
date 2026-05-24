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
    # Personas live on disk now (FilesystemPersonaRepository). A per-test
    # tmp dir keeps writes isolated; tests that need pre-seeded personas
    # use `ensure_user_personas(personas_dir)` to copy the shipped ones in.
    personas_dir = tmp_path / "personas"
    personas_dir.mkdir(exist_ok=True)
    conn = await init_db(db_path)
    try:
        yield SqliteRepositories(conn, personas_dir=personas_dir)
    finally:
        await conn.close()


@pytest.fixture
def object_store(tmp_path: Path) -> Path:
    p = tmp_path / "object_store"
    p.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture(autouse=True)
def _default_fake_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared `fake_entry()` helper defaults to `auth_env=FAKE_API_KEY`.
    The router now filters unreachable entries from candidate lists
    (see lyre/runtime/model_router.py — the second-pass reachability
    filter), so tests that don't care about auth still need the
    referenced env var to be set or every candidate gets dropped.

    Set it once here. Tests that DO want to exercise "key missing"
    behavior (e.g. adapter_factory crash paths, reachability tests)
    `monkeypatch.delenv("FAKE_API_KEY", raising=False)` inside the
    test — the monkeypatch's per-test scope reverts cleanly.
    """
    monkeypatch.setenv("FAKE_API_KEY", "fake-key-for-tests")
