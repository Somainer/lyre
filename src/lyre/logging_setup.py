"""Process-wide logging configuration (F2 observability).

Before this module, structlog ran on its import-time defaults: pretty
console lines to stdout, nothing persisted. A `lyre serve` running as a
daemon (or each `lyre run-task` wakeup subprocess, whose stdout the
scheduler discards save a 512-byte stderr tail) left no trace of its
WARNINGs — every incident investigation started from "whatever scrolled
past in the terminal".

`configure_logging()` routes structlog through stdlib logging with two
sinks:

  console   — human-readable, stdout, same look as before. Kept on
              stdout (not stderr) deliberately: tests assert on captured
              stdout, and operators pipe `lyre serve | tee`.
  file      — JSONL (one structlog event dict per line) under
              <lyre_home>/logs/lyre.jsonl, so incidents are a grep/jq
              away. Foreign stdlib records (uvicorn, anthropic SDK)
              flow into the same file.

Rotation and the multi-process story: the long-lived `lyre serve` /
`lyre dashboard` process owns rotation (RotatingFileHandler). Wakeup
subprocesses write the SAME file but must never rotate it themselves —
two processes rotating concurrently double-rename — so they use
WatchedFileHandler (rotate=False), which stat()s the inode per emit and
reopens after the serve process rotates the file out from under them.
Worst case at the rotation instant, a line lands in the just-rotated
backup — acceptable for observability data (logs are not the durable
truth; the kill-test never depends on them).
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

import structlog
from structlog.typing import Processor

LOG_FILE_NAME = "lyre.jsonl"

# Shared pre-processing for BOTH structlog-native events and foreign
# stdlib records, applied before the per-sink renderer.
_PRE_CHAIN: list[Processor] = [
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso"),
]


class _CurrentStdoutHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """StreamHandler that resolves ``sys.stdout`` at EMIT time, not at
    configure time. A plain StreamHandler captures the stream reference
    once — anything that swaps sys.stdout afterwards (pytest's capsys,
    output redirection) silently stops seeing the console log. This
    matches the lookup-on-write behavior of structlog's default
    PrintLogger, which the project's stdout-asserting tests rely on."""

    def emit(self, record: logging.LogRecord) -> None:
        self.stream: Any = sys.stdout
        super().emit(record)


def configure_logging(
    *,
    level: str = "INFO",
    log_dir: Path | None = None,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
    rotate: bool = True,
) -> Path | None:
    """Configure structlog + stdlib root logger. Returns the log file
    path (None when file logging is disabled via ``log_dir=None``).

    ``rotate=False`` selects WatchedFileHandler for subprocesses — see
    the module docstring for the multi-process rotation story. Safe to
    call more than once (replaces root handlers, doesn't stack them).
    """
    structlog.configure(
        processors=[
            *_PRE_CHAIN,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level, logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )

    console = _CurrentStdoutHandler(sys.stdout)
    console.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(
                colors=sys.stderr.isatty()
            ),
            foreign_pre_chain=_PRE_CHAIN,
        )
    )
    handlers: list[logging.Handler] = [console]

    log_path: Path | None = None
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / LOG_FILE_NAME
        file_handler: logging.Handler
        if rotate:
            file_handler = logging.handlers.RotatingFileHandler(
                log_path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
        else:
            file_handler = logging.handlers.WatchedFileHandler(
                log_path, encoding="utf-8"
            )
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=structlog.processors.JSONRenderer(
                    ensure_ascii=False
                ),
                foreign_pre_chain=_PRE_CHAIN,
            )
        )
        handlers.append(file_handler)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    # Replace (not append) so repeated configuration — group callback
    # then the serve/run-task entry point — doesn't double-emit.
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    for h in handlers:
        root.addHandler(h)
    return log_path
