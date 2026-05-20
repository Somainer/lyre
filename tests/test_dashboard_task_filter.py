"""Dashboard task-event filtering.

Scheduler-injected auto-wake tasks (the "Check your inbox: …" boilerplate
created when mail arrives for an idle agent) used to produce a `task
<id> (leader) → completed` activity row right next to the wakeup_end row
that already conveys the same info. The dashboard now suppresses these
auto-wake task events so the operator sees one row per wakeup, not two.

Dispatched (substantive) tasks must still emit their event — the agent
delegating work IS a meaningful audit signal.
"""

from __future__ import annotations

from datetime import datetime

from lyre.dashboard.activity import _AUTO_INBOX_GOAL_PREFIX, _build_task_events
from lyre.persistence.models import Task


def _make_task(goal: str, status: str = "completed") -> Task:
    return Task(
        id="01957c9a-aaaa-bbbb-cccc-dddd00000001",
        persona_name="leader",
        agent_id="leader",
        status=status,
        goal=goal,
        acceptance="ok",
        created_at=datetime(2026, 5, 19, 17, 0, 0),
        updated_at=datetime(2026, 5, 19, 17, 5, 0),
    )


def test_auto_wake_task_events_are_suppressed() -> None:
    """The 'Check your inbox' scheduler task should NOT appear in the
    activity feed — the matching wakeup_end carries the operational
    info, and the task row was visual noise."""
    auto_wake_goal = (
        _AUTO_INBOX_GOAL_PREFIX +
        " to see your unread mail (listing only — titles + sizes; …)"
    )
    events = _build_task_events([_make_task(auto_wake_goal)])
    assert events == [], (
        "auto-wake task events must be suppressed — wakeup_end already "
        "covers them"
    )


def test_dispatched_task_events_still_emit() -> None:
    """A real dispatched task (substantive goal) MUST still appear.
    Delegation is a meaningful audit signal — the operator wants to
    see leader hand off work to a worker."""
    events = _build_task_events([
        _make_task("Fix the typo in README.md per spec at /tmp/spec.md")
    ])
    assert len(events) == 1
    assert events[0].kind == "task"
    assert events[0].detail["status"] == "completed"
    assert "README" in events[0].detail["goal"]


def test_multiple_tasks_mixed_suppression() -> None:
    """Filter applies per-task: in a batch, auto-wake suppressed but
    dispatched survive."""
    tasks = [
        _make_task(_AUTO_INBOX_GOAL_PREFIX + " for unread mail"),
        _make_task("Real dispatched goal: open a PR"),
        _make_task(_AUTO_INBOX_GOAL_PREFIX + " again"),
    ]
    events = _build_task_events(tasks)
    assert len(events) == 1
    assert "Real dispatched goal" in events[0].detail["goal"]
