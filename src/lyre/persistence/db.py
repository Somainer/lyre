"""Database connection management.

SQLite via aiosqlite with WAL mode and the standard PRAGMA from PERSISTENCE_SCHEMA §3.5.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite

PRAGMAS = [
    "PRAGMA journal_mode = WAL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA temp_store = MEMORY",
    "PRAGMA busy_timeout = 10000",
]


async def open_db(path: str | Path) -> aiosqlite.Connection:
    """Open a connection with Lyre's standard PRAGMA setup."""
    conn = await aiosqlite.connect(str(path))
    for pragma in PRAGMAS:
        await conn.execute(pragma)
    await conn.commit()
    conn.row_factory = aiosqlite.Row
    return conn


def _migrations_dir() -> Path:
    """Migrations live at <project_root>/migrations/. The package lives at
    <project_root>/src/lyre/persistence/. Walk up to find it."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "migrations"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("migrations/ directory not found")


def _list_migrations() -> list[Path]:
    """`<digits>_<name>.sql` files, sorted by version."""
    files = [
        p for p in _migrations_dir().iterdir()
        if p.is_file() and p.suffix == ".sql" and p.stem[:1].isdigit()
    ]
    return sorted(files, key=_version_of)


def _version_of(path: Path) -> int:
    """`0002_broadcast.sql` → 2"""
    return int(path.stem.split("_", 1)[0])


async def apply_migrations(conn: aiosqlite.Connection) -> None:
    """Apply all pending migrations idempotently.

    Convention:
      - 0001_initial.sql is always run (its CREATE TABLE IF NOT EXISTS makes
        re-execution safe; it also creates schema_migrations and seeds
        version=1).
      - Every later 0NNN_*.sql is run only if `schema_migrations` lacks the
        corresponding version row. Each one is committed atomically.
    """
    files = _list_migrations()
    if not files:
        return

    first = files[0]
    if _version_of(first) != 1:
        raise RuntimeError(
            f"first migration must be version 1, got {first.name!r}"
        )
    await conn.executescript(first.read_text(encoding="utf-8"))
    await conn.commit()

    for f in files[1:]:
        v = _version_of(f)
        async with conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?", (v,)
        ) as cur:
            row = await cur.fetchone()
        if row is not None:
            continue
        await conn.executescript(f.read_text(encoding="utf-8"))
        await conn.execute(
            "INSERT INTO schema_migrations (version) VALUES (?)", (v,)
        )
        await conn.commit()


async def init_db(path: str | Path) -> aiosqlite.Connection:
    """Open the connection AND apply any pending migrations.

    Migrations are idempotent (gated by `schema_migrations`), so it's safe —
    and the policy — to call this on every CLI entry. That way upgrading
    Lyre's schema doesn't require a separate "migrate" command; the next
    `lyre serve` (or any CLI) silently brings the DB up to date.
    """
    conn = await open_db(path)
    await apply_migrations(conn)
    return conn
