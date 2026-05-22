"""Regression tests for the failure-report tracked at
``docs/design/`` — coupled issues uncovered when a real analyst
wakeup wedged at max_turns and the post-loop write hit the
``tasks.status`` CHECK constraint, plus the follow-up phantom-
delegation report.

The fixes verified here:

P0  Scheduler maps ``AgentLoopResult.status`` to a valid ``TaskStatus``
    before writing — ``needs_continuation`` no longer trips the DB.
P1  ``max_tokens`` is plumbed from ``Config`` into ``AgentLoop`` so
    operators can lift the per-turn output budget (the default 4096
    was small enough that long tool-call arguments would truncate
    mid-JSON).
P2  ``AgentLoop._dispatch_tool`` recognises the adapter's ``_raw``
    fallback (set when the JSON parse fails — usually because of
    truncation) and surfaces a specific error so the model breaks
    out of the retry-with-same-malformed-args loop.
P3  ``_check_phantom_delegation`` warns when a wakeup mailed the owner
    without successfully dispatching anything in the same wakeup
    (the body almost certainly claims work that didn't happen).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyre.config import Config
from lyre.persistence.models import TaskStatus
from lyre.runtime.agent_loop import AgentLoop
from lyre.runtime.health_tracker import HealthTracker
from lyre.runtime.transcript import TranscriptWriter
from lyre.scheduler.scheduler import _wakeup_status_to_task_status

from .helpers import fake_entry

# ---------------------------------------------------------------------------
# P0: wakeup-status → task-status mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "wakeup_status,expected_task_status",
    [
        ("completed", "completed"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
        ("silent_close", "completed"),
        # The bug: ``needs_continuation`` is a wakeup-only status; it
        # must NOT flow into ``tasks.status``. Map it to ``failed`` so
        # the scheduler doesn't blindly retry a wedged task and burn
        # tokens forever, AND so the post-loop write succeeds.
        ("needs_continuation", "failed"),
    ],
)
def test_wakeup_status_maps_to_valid_task_status(
    wakeup_status: str, expected_task_status: str,
) -> None:
    mapped = _wakeup_status_to_task_status(wakeup_status)
    assert mapped == expected_task_status
    # And the mapped value MUST be in the literal TaskStatus set —
    # i.e. would pass the DB CHECK constraint.
    assert mapped in TaskStatus.__args__  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# P1: max_tokens is configurable
# ---------------------------------------------------------------------------


def test_max_tokens_defaults_to_32k_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """32k is the floor for per-turn output. max_tokens is a single-
    turn cap, not a lifetime budget — what bounds it is the biggest
    single tool-call argument an agent writes (worker_maintainer
    writing code via python_exec is the hot path, easily 5–20k).
    4096 / 8192 truncated mid-JSON for those callers; 32k is
    generous on every modern flagship."""
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LYRE_MAX_TOKENS", raising=False)
    cfg = Config.from_env()
    assert cfg.max_tokens == 32768


def test_max_tokens_reads_from_runtime_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LYRE_MAX_TOKENS", raising=False)
    (tmp_path / "config.toml").write_text(
        '[owner]\nname = "o"\n\n[runtime]\nmax_tokens = 16384\n',
        encoding="utf-8",
    )
    assert Config.from_env().max_tokens == 16384


def test_max_tokens_env_overrides_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LYRE_MAX_TOKENS", "32768")
    (tmp_path / "config.toml").write_text(
        '[owner]\nname = "o"\n\n[runtime]\nmax_tokens = 16384\n',
        encoding="utf-8",
    )
    assert Config.from_env().max_tokens == 32768


def test_max_tokens_floors_at_256_for_garbage_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Misconfigured ``max_tokens=0`` (or negative) should not starve
    every wakeup. Clamp to a small-but-functional floor."""
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LYRE_MAX_TOKENS", raising=False)
    (tmp_path / "config.toml").write_text(
        '[owner]\nname = "o"\n\n[runtime]\nmax_tokens = 0\n',
        encoding="utf-8",
    )
    assert Config.from_env().max_tokens == 256


# ---------------------------------------------------------------------------
# P2: adapter ``_raw`` fallback is detected at dispatch time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_tool_detects_raw_fallback(tmp_path: Path) -> None:
    """When the adapter couldn't parse the model's tool-call arguments
    (truncation by max_tokens, malformed JSON, etc.) it returns
    ``{"_raw": <partial-string>}``. The agent loop must NOT pass this
    through to the per-tool handler — that just produces a generic
    "provide 'code'" error and the model keeps re-issuing the same
    malformed call until max_turns. Instead surface a specific error
    so the model knows to shrink the call."""
    from lyre.runtime.tools import ToolContext, ToolRegistry
    from lyre.runtime.tools.builtin import build_default_registry

    object_store = tmp_path / "objstore"
    object_store.mkdir()
    transcript = TranscriptWriter(object_store, "wakeup-truncated")
    registry: ToolRegistry = build_default_registry()
    # ToolContext below stays unused because the dispatch should fail
    # BEFORE the handler is invoked. ``repos=None`` would be a type
    # error in handlers, but we never reach them.
    ctx = ToolContext(
        repos=None,  # type: ignore[arg-type]
        task_id="t-unused", wakeup_id="w-unused",
        persona_name="worker-maintainer", agent_id="worker-maintainer/x",
    )
    loop = AgentLoop(
        candidates=[fake_entry(id="a.flagship", tier="flagship")],
        adapter_for=lambda e: None,  # type: ignore[arg-type, return-value]
        model_name_for=lambda e: e.id,
        transcript=transcript,
        tool_registry=registry,
        tool_context=ctx,
        allowed_tools=["python_exec"],
        max_tokens=4096,
        health=HealthTracker(),
    )

    out, is_error = await loop._dispatch_tool(
        name="python_exec",
        tool_use_id="tu_truncated",
        tool_input={"_raw": '{"code": "print(\\"hello'},  # truncated JSON
    )

    transcript.close()
    assert is_error is True
    assert "malformed arguments" in out
    assert "max_tokens=4096" in out
    # The model should be told NOT to retry the same call — that
    # advice is the loop-break.
    assert "Do NOT retry" in out


@pytest.mark.asyncio
async def test_dispatch_tool_passes_through_well_formed_args(
    tmp_path: Path,
) -> None:
    """Sanity check: a tool_input that is NOT the ``{"_raw": ...}``
    sentinel must reach the per-tool handler normally. ``_raw`` as
    a legitimate key alongside others is also fine — only the
    standalone ``{"_raw": <str>}`` shape is the truncation
    indicator the adapters emit."""
    from lyre.runtime.tools import ToolContext, ToolRegistry
    from lyre.runtime.tools.builtin import build_default_registry

    object_store = tmp_path / "objstore"
    object_store.mkdir()
    transcript = TranscriptWriter(object_store, "wakeup-ok")
    registry: ToolRegistry = build_default_registry()
    ctx = ToolContext(
        repos=None,  # type: ignore[arg-type]
        task_id="t", wakeup_id="w",
        persona_name="worker-maintainer", agent_id="worker-maintainer/x",
    )
    loop = AgentLoop(
        candidates=[fake_entry(id="a.flagship", tier="flagship")],
        adapter_for=lambda e: None,  # type: ignore[arg-type, return-value]
        model_name_for=lambda e: e.id,
        transcript=transcript,
        tool_registry=registry,
        tool_context=ctx,
        allowed_tools=["python_exec"],
        health=HealthTracker(),
    )

    # ``_raw`` alongside other keys → not the sentinel, falls through
    # to the handler (which will then complain about the missing
    # ``code`` field — that's the handler's job, not the dispatch
    # layer's).
    out, is_error = await loop._dispatch_tool(
        name="python_exec",
        tool_use_id="tu_mixed",
        tool_input={"_raw": "leftover bytes", "code": "print(1)"},
    )
    transcript.close()
    # python_exec without ``repos`` won't run cleanly, but the key
    # invariant is: we did NOT short-circuit with the truncation
    # error — the handler was actually reached.
    assert "malformed arguments" not in out


# ---------------------------------------------------------------------------
# P3: phantom-delegation observability
# ---------------------------------------------------------------------------


def _tu(name: str, **input_kwargs: object) -> dict[str, object]:
    """Shorthand for a tool-call dict matching the AgentLoop shape."""
    return {"name": name, "id": f"tu_{name}", "input": dict(input_kwargs)}


def test_phantom_delegation_warns_on_owner_mail_without_successful_dispatch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The failure case from the report: create_agent fails twice,
    list_models fails, then the model still sends owner mail
    promising delegation. No successful dispatch_task — the warning
    must fire so the pattern is discoverable in retro.

    structlog writes to stdout in this project, not via stdlib
    logging, so we read capsys, not caplog.
    """
    from lyre.runtime.agent_loop import _check_phantom_delegation

    calls = [
        _tu("create_agent", persona="worker-maintainer", model=""),
        _tu("create_agent", persona="worker-maintainer", model="bad"),
        _tu("list_models"),
        _tu("mailbox_send", to="owner", body="will dispatch soon"),
    ]
    outcomes = [True, True, True, False]  # all but the final send errored

    _check_phantom_delegation(calls, outcomes)

    captured = capsys.readouterr()
    assert "phantom_delegation_suspected" in captured.out


def test_phantom_delegation_silent_when_dispatch_actually_succeeded(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Happy path: model successfully dispatches AND mails the owner.
    No warning should fire."""
    from lyre.runtime.agent_loop import _check_phantom_delegation

    calls = [
        _tu("create_agent", persona="worker-maintainer"),
        _tu("dispatch_task", agent="worker-maintainer/x", goal="g",
            acceptance="a"),
        _tu("mailbox_send", to="owner", body="dispatched, task_id=..."),
    ]
    outcomes = [False, False, False]

    _check_phantom_delegation(calls, outcomes)

    captured = capsys.readouterr()
    assert "phantom_delegation_suspected" not in captured.out


def test_phantom_delegation_silent_when_owner_mail_is_legit_ack(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A wakeup that just reads + acks owner mail (no attempted
    delegation at all) should NOT warn — the body is presumably a
    legit "got your message" not a fake delegation claim. The
    heuristic only fires when the model TRIED to delegate."""
    from lyre.runtime.agent_loop import _check_phantom_delegation

    calls = [
        _tu("mailbox_read", min_urgency="normal"),
        _tu("mailbox_send", to="owner", body="acknowledged, no action"),
    ]
    outcomes = [False, False]

    _check_phantom_delegation(calls, outcomes)

    captured = capsys.readouterr()
    assert "phantom_delegation_suspected" not in captured.out


def test_phantom_delegation_ignores_peer_directed_mail(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mail to a non-owner recipient (peer agent) doesn't count
    toward the heuristic — peer-to-peer chatter without dispatch
    is normal and isn't the failure pattern we're guarding."""
    from lyre.runtime.agent_loop import _check_phantom_delegation

    calls = [
        _tu("create_agent", persona="worker-maintainer"),
        _tu("mailbox_send", to="analyst-1", body="please research X"),
    ]
    outcomes = [True, False]  # create_agent failed, mail went out

    _check_phantom_delegation(calls, outcomes)

    captured = capsys.readouterr()
    assert "phantom_delegation_suspected" not in captured.out
