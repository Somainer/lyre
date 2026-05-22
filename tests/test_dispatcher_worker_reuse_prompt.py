"""Pin the dispatcher prompt's "reuse worker, don't spawn per task" rule.

Without this guard a well-meaning prompt edit can easily slip back
into encouraging task-scoped worker names (e.g.
``worker-maintainer/refactor-auth``), which the dispatcher then reads
as "this worker is for the auth refactor only" and spawns a fresh
agent for every subsequent task. In the wild that produces unbounded
agent growth — each agent never seeing more than one task, its
agent-notes file empty forever, lineage view drowning in dead rows.

This file does not test the model's behaviour (that would need a real
wakeup). It just checks the prompt SAYS the right thing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_DISPATCHER_MD = (
    Path(__file__).parent.parent
    / "src" / "lyre" / "personas" / "dispatcher.md"
)


@pytest.fixture
def dispatcher_body() -> str:
    return _DISPATCHER_MD.read_text(encoding="utf-8")


def test_dispatcher_promotes_worker_reuse_over_spawn(
    dispatcher_body: str,
) -> None:
    """The reuse-first framing must be present. Two signals: the
    long-term-specialist headline, AND the explicit "available 就是
    默认派发对象" rule. Either drifting away will regress the
    unbounded-agent behaviour."""
    assert "worker 是长期专家" in dispatcher_body, (
        "dispatcher.md lost the 'workers are long-term specialists' "
        "framing — the prompt slips back into per-task naming and "
        "unbounded agent growth without it"
    )
    assert "默认的派发对象" in dispatcher_body, (
        "dispatcher.md lost the explicit 'available agent IS the "
        "default dispatch target' rule"
    )


def test_dispatcher_warns_against_task_scoped_names(
    dispatcher_body: str,
) -> None:
    """The prompt must call out the failure mode by name — without
    a concrete anti-example the model regresses to the friendly
    "give it a descriptive name like refactor-auth" pattern that
    used to be in this file."""
    assert "**不要**用任务名字" in dispatcher_body
    # Anti-example is what makes the rule sticky.
    assert "refactor-auth" in dispatcher_body
    assert "本次目标" in dispatcher_body


def test_dispatcher_no_longer_prescribes_task_scoped_naming(
    dispatcher_body: str,
) -> None:
    """The old guidance — "名字必须有信息量，refactor-auth / pr-142
    / dep-upgrade" — actively promoted task-scoped names. It MUST
    NOT come back."""
    forbidden = "名字必须有信息量"
    assert forbidden not in dispatcher_body, (
        f"dispatcher.md regressed to the old prescriptive-naming "
        f"rule ({forbidden!r}); see the worker-reuse design note "
        f"in this test file's module docstring."
    )
