"""The long-runner persona's load-bearing invariants.

long-runner is the sustained single-goal coordinator that takes long-running
work OFF the owner-facing dispatcher (which is deliberately single-step). Two
things must hold for it to behave as designed — these are structural, not
prose checks:

  - kind == spawn_only: it must NOT be bootstrap-seeded (the dispatcher spawns
    one per long goal). A regression to seeded/singleton would auto-create a
    standing long-runner agent nobody asked for.
  - it's a COORDINATOR, not an executor: it can self-checkpoint and drive
    sub-work, but has no shell_exec/python_exec (code/research is delegated,
    same boundary as the dispatcher).
"""

from __future__ import annotations

from pathlib import Path

import lyre.personas as personas_pkg
from lyre.personas.seed import load_persona_from_file


def _long_runner():
    return load_persona_from_file(Path(personas_pkg.__file__).parent / "long-runner.md")


def test_long_runner_is_spawn_only() -> None:
    # parses (frontmatter intact) AND won't get a default bootstrap agent.
    assert _long_runner().kind == "spawn_only"


def test_long_runner_can_drive_and_checkpoint_but_not_execute() -> None:
    tools = set(_long_runner().allowed_lyre_tools)
    # the driver loop: self-checkpoint + self-scheduled re-wake + drive sub-work.
    for needed in ("report_progress", "mailbox_send", "dispatch_task", "create_agent"):
        assert needed in tools, needed
    # coordinator, not executor — code/research is delegated to worker/analyst.
    assert "shell_exec" not in tools
    assert "python_exec" not in tools
