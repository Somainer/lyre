"""Tests for the `lyre send` CLI command.

CliRunner runs commands synchronously and the CLI itself opens its own asyncio
loop, so we must NOT decorate these tests with @pytest.mark.asyncio. We use
plain sqlite3 to verify side effects.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from lyre.main import cli


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[CliRunner, Path]:
    """A bootstrapped Lyre environment for CLI tests.

    Calls ``bootstrap_runtime`` directly (the non-interactive subset of
    ``lyre onboard``) so the CLI fixture stays sync-friendly and doesn't
    require feeding the interactive wizard prompts.
    """
    import asyncio

    from lyre.config import Config
    from lyre.onboard import bootstrap_runtime

    db_path = tmp_path / "lyre.db"
    obj = tmp_path / "objstore"
    home = tmp_path / "lyre_home"
    monkeypatch.setenv("LYRE_HOME", str(home))
    monkeypatch.setenv("LYRE_DB_PATH", str(db_path))
    monkeypatch.setenv("LYRE_OBJECT_STORE", str(obj))
    monkeypatch.chdir(tmp_path)  # so dotenv loader doesn't pick up project .env

    cfg = Config.from_env()
    asyncio.run(bootstrap_runtime(cfg))

    runner = CliRunner()
    return runner, db_path


def _rows(db_path: Path, sql: str, *params) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute(sql, params))
    finally:
        conn.close()


def test_send_writes_message_with_defaults(cli_env: tuple[CliRunner, Path]) -> None:
    runner, db_path = cli_env
    result = runner.invoke(cli, ["send", "leader", "hello leader"])
    assert result.exit_code == 0, result.output
    assert "sent" in result.output
    assert "normal" in result.output
    assert "owner → leader" in result.output

    rows = _rows(
        db_path,
        "SELECT * FROM mailbox_messages WHERE recipient='leader'",
    )
    assert len(rows) == 1
    assert rows[0]["body"] == "hello leader"
    assert rows[0]["urgency"] == "normal"
    assert rows[0]["sender"] == "owner"


def test_send_with_urgency_and_sender(cli_env: tuple[CliRunner, Path]) -> None:
    runner, db_path = cli_env
    # Post-A3: `lyre send` validates recipient is a real agent. Create a
    # worker agent first, then send to it.
    create = runner.invoke(
        cli, ["agent", "create", "worker-maintainer", "--name", "worker-1"]
    )
    assert create.exit_code == 0, create.output
    result = runner.invoke(
        cli,
        [
            "send", "worker-1", "STOP, recheck constraints",
            "--urgency", "blocker", "--from", "leader",
        ],
    )
    assert result.exit_code == 0, result.output

    rows = _rows(
        db_path,
        "SELECT * FROM mailbox_messages WHERE recipient=? AND urgency='blocker'",
        "worker-1",
    )
    assert len(rows) == 1
    assert rows[0]["sender"] == "leader"
    assert "STOP" in rows[0]["body"]


def test_send_rejects_unknown_agent(cli_env: tuple[CliRunner, Path]) -> None:
    """Mail to an agent id that doesn't exist must error cleanly, not
    silently create a phantom mailbox (matches the in-tool validation
    that closes the 'leader-scheduler' hallucination bug)."""
    runner, _ = cli_env
    result = runner.invoke(cli, ["send", "ghost-agent", "hi"])
    assert result.exit_code != 0
    assert "unknown agent" in result.output.lower()


def test_send_rejects_invalid_urgency(cli_env: tuple[CliRunner, Path]) -> None:
    runner, _ = cli_env
    result = runner.invoke(cli, ["send", "leader", "x", "--urgency", "panic"])
    assert result.exit_code != 0
    assert "panic" in result.output.lower() or "invalid" in result.output.lower()


def test_send_attaches_task_id(cli_env: tuple[CliRunner, Path]) -> None:
    runner, db_path = cli_env
    # Pre-insert a task directly (the schema CHECKs status against an enum;
    # the FK on tasks.persona_name has been satisfied by `lyre onboard` seeding).
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO tasks (id, persona_name, goal, acceptance, status) "
            "VALUES ('task-fk-1', 'leader', 'g', 'a', 'pending')"
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(
        cli, ["send", "leader", "fyi", "--task-id", "task-fk-1"],
    )
    assert result.exit_code == 0, result.output

    rows = _rows(
        db_path,
        "SELECT task_id FROM mailbox_messages WHERE recipient='leader'",
    )
    assert any(r["task_id"] == "task-fk-1" for r in rows)


def test_send_external_id_is_unique_per_invocation(
    cli_env: tuple[CliRunner, Path],
) -> None:
    """Two identical `lyre send` invocations produce two rows (different
    uuid-based external_ids); we deliberately do not dedupe at the CLI layer
    since the caller is a human typing the same thing on purpose."""
    runner, db_path = cli_env
    runner.invoke(cli, ["send", "leader", "ping"])
    runner.invoke(cli, ["send", "leader", "ping"])
    rows = _rows(
        db_path, "SELECT external_id FROM mailbox_messages WHERE recipient='leader'"
    )
    assert len(rows) == 2
    assert rows[0]["external_id"] != rows[1]["external_id"]
