"""configure_logging: the F2 observability contract — structlog events
reach BOTH the human console (stdout, unchanged dev experience) and a
JSONL file; the serve process rotates, subprocesses follow rotation.

Global-state caution: these tests reconfigure structlog + the stdlib
root logger, so every test runs under `_restore_logging` which resets
both — otherwise later tests that assert on structlog's default
stdout behavior (capsys-based) would inherit our handlers."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog

from lyre.logging_setup import LOG_FILE_NAME, configure_logging


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    prior_level = root.level
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    root.setLevel(prior_level)
    structlog.reset_defaults()


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_event_lands_in_file_as_json_and_on_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    log_path = configure_logging(log_dir=tmp_path)
    assert log_path == tmp_path / LOG_FILE_NAME

    structlog.get_logger("t1").warning("tool_args_truncated", raw_len=7575)

    rows = _read_jsonl(log_path)
    assert len(rows) == 1
    assert rows[0]["event"] == "tool_args_truncated"
    assert rows[0]["raw_len"] == 7575
    assert rows[0]["level"] == "warning"
    assert "timestamp" in rows[0]
    # Console keeps working — same event, human form, on STDOUT (tests
    # and operators in this project read stdout, not stderr).
    out = capsys.readouterr().out
    assert "tool_args_truncated" in out


def test_foreign_stdlib_records_reach_the_file(tmp_path: Path) -> None:
    """uvicorn / SDK logs go through stdlib logging, not structlog —
    they must land in the same file via the foreign pre-chain."""
    log_path = configure_logging(log_dir=tmp_path)
    logging.getLogger("uvicorn.error").warning("connection dropped")
    rows = _read_jsonl(log_path)
    assert any(
        r["event"] == "connection dropped" and r["logger"] == "uvicorn.error"
        for r in rows
    )


def test_level_filters_both_sinks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    log_path = configure_logging(level="WARNING", log_dir=tmp_path)
    structlog.get_logger("t3").info("too_quiet")
    structlog.get_logger("t3").error("loud_enough")
    rows = _read_jsonl(log_path)
    assert [r["event"] for r in rows] == ["loud_enough"]
    assert "too_quiet" not in capsys.readouterr().out


def test_rotation_caps_the_file(tmp_path: Path) -> None:
    log_path = configure_logging(
        log_dir=tmp_path, max_bytes=2048, backup_count=2,
    )
    logger = structlog.get_logger("t4")
    for i in range(100):
        logger.info("filler", n=i, pad="x" * 100)
    assert log_path.stat().st_size <= 2048
    assert (tmp_path / f"{LOG_FILE_NAME}.1").exists()
    # backup_count bounds total disk: at most .1 and .2 exist.
    assert not (tmp_path / f"{LOG_FILE_NAME}.3").exists()


def test_subprocess_mode_follows_external_rotation(tmp_path: Path) -> None:
    """rotate=False (wakeup subprocess): when the serve process rotates
    the shared file out from under us, the WatchedFileHandler reopens
    the canonical path on the next emit instead of writing forever into
    the renamed backup."""
    log_path = configure_logging(log_dir=tmp_path, rotate=False)
    logger = structlog.get_logger("t5")
    logger.info("before_rotation")
    assert log_path is not None
    log_path.rename(tmp_path / f"{LOG_FILE_NAME}.1")  # serve rotates

    logger.info("after_rotation")
    rows = _read_jsonl(log_path)  # the recreated canonical file
    assert [r["event"] for r in rows] == ["after_rotation"]


def test_reconfiguration_replaces_handlers_not_stacks(
    tmp_path: Path,
) -> None:
    """The CLI group configures console-only, then serve/run-task
    reconfigure with the file sink — events must not double-emit."""
    configure_logging(log_dir=None)
    log_path = configure_logging(log_dir=tmp_path)
    structlog.get_logger("t6").info("exactly_once")
    rows = _read_jsonl(log_path)
    assert len(rows) == 1


def test_config_parses_logging_section_with_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lyre.config import Config

    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for var in (
        "LYRE_LOG_LEVEL", "LYRE_LOG_DIR", "LYRE_LOG_MAX_BYTES",
        "LYRE_LOG_BACKUPS", "LYRE_LOG_TO_FILE",
    ):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "config.toml").write_text(
        '[owner]\nname = "o"\n\n'
        '[logging]\nlevel = "debug"\nmax_bytes = 2048\nbackup_count = 1\n',
        encoding="utf-8",
    )
    cfg = Config.from_env()
    assert cfg.log_level == "DEBUG"  # normalized upper
    assert cfg.log_max_bytes == 2048
    assert cfg.log_backup_count == 1
    assert cfg.log_to_file is True
    assert cfg.log_dir == tmp_path / "logs"

    monkeypatch.setenv("LYRE_LOG_LEVEL", "garbage")
    monkeypatch.setenv("LYRE_LOG_TO_FILE", "0")
    monkeypatch.setenv("LYRE_LOG_DIR", str(tmp_path / "elsewhere"))
    cfg = Config.from_env()
    assert cfg.log_level == "INFO"  # garbage → default, not a crash
    assert cfg.log_to_file is False
    assert cfg.log_dir == tmp_path / "elsewhere"


def test_config_zero_footguns_are_floored_consistently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """backup_count=0 with rotation on means an unbounded file plus a
    close/reopen on every emit past the threshold — floored to 1
    (disabling the file sink is LYRE_LOG_TO_FILE's job). And a toml 0
    must behave like the env string "0" (floored), not silently fall
    back to the default through an `or` chain."""
    from lyre.config import Config

    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for var in ("LYRE_LOG_MAX_BYTES", "LYRE_LOG_BACKUPS"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "config.toml").write_text(
        '[owner]\nname = "o"\n\n[logging]\nmax_bytes = 0\nbackup_count = 0\n',
        encoding="utf-8",
    )
    cfg = Config.from_env()
    assert cfg.log_max_bytes == 1024  # floored, not defaulted to 10MB
    assert cfg.log_backup_count == 1

    monkeypatch.setenv("LYRE_LOG_MAX_BYTES", "0")
    monkeypatch.setenv("LYRE_LOG_BACKUPS", "0")
    cfg = Config.from_env()
    assert cfg.log_max_bytes == 1024  # env "0" — same floor
    assert cfg.log_backup_count == 1
