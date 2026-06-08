"""C4: bounded DB maintenance — prune terminal/delivered rows past a retention
window, then reclaim space (WAL checkpoint + optional VACUUM).

The DB-side dual of RB-3 (filesystem-side notes rotation). NEVER touches
``mailbox_messages`` (铁律五: owner/peer comms kept verbatim), ``blobs``, or
``artifacts`` (referenced content); cold transcripts live on the filesystem,
not here. Deletes are idempotent age-window DELETEs — a SIGKILL mid-run just
means the next run finishes the job (the rows are already terminal/delivered).
See LONG_RUNNING_ROBUSTNESS_3.md C4.
"""

from __future__ import annotations

import aiosqlite
import structlog

log = structlog.get_logger()

# Keep at least this many most-recent wakeups per agent regardless of age, so an
# agent's recent audit trail survives even under an aggressive retention window.
_KEEP_WAKEUPS_PER_AGENT = 50

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now', ?)"


async def run_maintenance(
    conn: aiosqlite.Connection,
    *,
    retention_days: int,
    vacuum: bool = False,
    keep_wakeups_per_agent: int = _KEEP_WAKEUPS_PER_AGENT,
) -> dict[str, int]:
    """Prune rows older than ``retention_days``, checkpoint the WAL, optionally
    VACUUM. Returns per-table delete counts. No-op (zeros) when
    ``retention_days <= 0``."""
    counts = {"outbox": 0, "wakeups": 0, "scheduled_mail": 0, "fan_in_groups": 0}
    if retention_days <= 0:
        return counts
    age = f"-{retention_days} days"

    # 1) outbox: delivered rows (dispatched_at set) past the window. FIRST, so
    #    the wakeups purge below isn't blocked by the outbox.wakeup_id FK.
    async with conn.execute(
        f"DELETE FROM outbox WHERE dispatched_at IS NOT NULL "
        f"AND dispatched_at < {_NOW}",
        (age,),
    ) as cur:
        counts["outbox"] = cur.rowcount or 0

    # 2) wakeups: ended rows past the window, KEEPING the most-recent K per
    #    agent, and never one still referenced by a (surviving) outbox row.
    async with conn.execute(
        f"""
        DELETE FROM wakeups
        WHERE ended_at IS NOT NULL
          AND ended_at < {_NOW}
          AND id NOT IN (SELECT wakeup_id FROM outbox WHERE wakeup_id IS NOT NULL)
          AND id IN (
            SELECT id FROM (
              SELECT id, ROW_NUMBER() OVER (
                       PARTITION BY agent_id ORDER BY started_at DESC
                     ) AS rn
              FROM wakeups WHERE ended_at IS NOT NULL
            ) WHERE rn > ?
          )
        """,
        (age, keep_wakeups_per_agent),
    ) as cur:
        counts["wakeups"] = cur.rowcount or 0

    # 3) scheduled_mail: terminal rows past the window.
    async with conn.execute(
        f"DELETE FROM scheduled_mail "
        f"WHERE status IN ('completed','cancelled','bounced') "
        f"AND created_at < {_NOW}",
        (age,),
    ) as cur:
        counts["scheduled_mail"] = cur.rowcount or 0

    # 4) fan_in: terminal groups past the window (members first for the FK).
    #    `async with` (no result) so the cursor is closed — an open statement
    #    would later block VACUUM with "SQL statements in progress".
    async with conn.execute(
        f"""
        DELETE FROM fan_in_members WHERE group_id IN (
          SELECT id FROM fan_in_groups
          WHERE status IN ('expired','cancelled','resolved')
            AND created_at < {_NOW}
        )
        """,
        (age,),
    ):
        pass
    async with conn.execute(
        f"DELETE FROM fan_in_groups "
        f"WHERE status IN ('expired','cancelled','resolved') "
        f"AND created_at < {_NOW}",
        (age,),
    ) as cur:
        counts["fan_in_groups"] = cur.rowcount or 0

    await conn.commit()

    # Reclaim space. wal_checkpoint(TRUNCATE) is cheap; full VACUUM locks +
    # rewrites the whole DB, so it's opt-in (the CLI does it; the periodic
    # scheduler phase doesn't). Both run with no open statement/transaction.
    async with conn.execute("PRAGMA wal_checkpoint(TRUNCATE)"):
        pass
    if vacuum:
        async with conn.execute("VACUUM"):
            pass
    await conn.commit()

    log.info("db_maintenance", retention_days=retention_days, vacuum=vacuum, **counts)
    return counts
