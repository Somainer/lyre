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
from lyre.persistence.models import Task, TaskStatus
from lyre.runtime.agent_loop import AgentLoop
from lyre.runtime.health_tracker import HealthTracker
from lyre.runtime.transcript import TranscriptWriter
from lyre.scheduler.scheduler import _effective_max_turns, _wakeup_status_to_task_status

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


def _raw_dispatch_loop(tmp_path: Path, name: str) -> tuple[AgentLoop, TranscriptWriter]:
    """A loop wired just far enough to exercise ``_dispatch_tool``.
    ``repos=None`` would be a type error in handlers, but the ``_raw``
    sentinel must short-circuit BEFORE any handler is invoked."""
    from lyre.runtime.tools import ToolContext, ToolRegistry
    from lyre.runtime.tools.builtin import build_default_registry

    object_store = tmp_path / "objstore"
    object_store.mkdir(exist_ok=True)
    transcript = TranscriptWriter(object_store, name)
    registry: ToolRegistry = build_default_registry()
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
    return loop, transcript


@pytest.mark.asyncio
async def test_dispatch_tool_detects_raw_fallback_cut_by_max_tokens(
    tmp_path: Path,
) -> None:
    """When the adapter couldn't parse the model's tool-call arguments it
    returns ``{"_raw": <partial-string>}``. The agent loop must NOT pass
    this through to the per-tool handler — that just produces a generic
    "provide 'code'" error and the model keeps re-issuing the same
    malformed call until max_turns. With the turn's measured
    stop_reason=max_tokens, the error must name the output budget as the
    cause and tell the model to split the payload (the 2026-06 field
    incident: 10KB whole-file writes cut mid-emission, 13 retries)."""
    loop, transcript = _raw_dispatch_loop(tmp_path, "wakeup-truncated")

    out, is_error, _view = await loop._dispatch_tool(
        name="python_exec",
        tool_use_id="tu_truncated",
        tool_input={"_raw": '{"code": "print(\\"hello'},  # truncated JSON
        stop_reason="max_tokens",
    )

    transcript.close()
    assert is_error is True
    assert "malformed arguments" in out
    assert "max_tokens=4096" in out
    # The model should be told NOT to retry the same call — that
    # advice is the loop-break — and HOW to proceed instead.
    assert "Do NOT retry" in out
    assert "append the rest" in out


@pytest.mark.asyncio
async def test_dispatch_tool_raw_fallback_without_max_tokens_blames_provider(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Same sentinel but the turn did NOT stop on max_tokens: the error
    must not assert the output-budget theory (the old hardcoded
    "probably truncated by max_tokens" misdiagnosed provider-side
    truncation) — it reports the measured stop_reason instead. The
    WARNING log line is the operator-facing half of the same diagnosis
    (structlog writes to stdout in this project, so capsys not caplog)."""
    loop, transcript = _raw_dispatch_loop(tmp_path, "wakeup-provider-trunc")

    out, is_error, _view = await loop._dispatch_tool(
        name="python_exec",
        tool_use_id="tu_truncated",
        tool_input={"_raw": '{"code": "print(\\"hello'},
        stop_reason="tool_use",
    )

    transcript.close()
    assert is_error is True
    assert "truncated or malformed from the provider" in out
    assert "stop_reason='tool_use'" in out
    assert "max_tokens=4096" not in out
    assert "Do NOT retry" in out
    captured = capsys.readouterr()
    assert "tool_args_truncated" in captured.out


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
    out, is_error, _view = await loop._dispatch_tool(
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
# S1: a budget-truncated turn's FINAL tool call is refused even when its
# arguments parse — constrained decoding / JSON-repairing gateways can close
# a string cut mid-emission into a VALID but silently shortened payload
# (the classic catastrophe: `rm /path/...` arriving as `rm /`).
# ---------------------------------------------------------------------------


def _recording_registry() -> tuple[object, list[dict[str, object]]]:
    from lyre.runtime.tools import Tool, ToolRegistry

    executed: list[dict[str, object]] = []

    async def _handler(ctx: object, args: dict[str, object]) -> str:
        executed.append({k: v for k, v in args.items() if k != "_tool_use_id"})
        return "ok"

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="probe",
            description="records executions",
            input_schema={"type": "object"},
            handler=_handler,
        )
    )
    return registry, executed


def _probe_loop(tmp_path: Path, adapter: object, wakeup_id: str):  # type: ignore[no-untyped-def]
    from lyre.runtime.tools import ToolContext

    from .helpers import build_single_candidate_loop

    registry, executed = _recording_registry()
    object_store = tmp_path / "objstore"
    object_store.mkdir(exist_ok=True)
    transcript = TranscriptWriter(object_store, wakeup_id)
    loop = build_single_candidate_loop(
        adapter, transcript, max_turns=10,
        tool_registry=registry,
        tool_context=ToolContext(
            repos=None,  # type: ignore[arg-type]
            task_id="t", wakeup_id="w",
            persona_name="worker-maintainer", agent_id="worker-maintainer/x",
        ),
        allowed_tools=["probe"],
    )
    return loop, transcript, executed


@pytest.mark.asyncio
async def test_truncated_turn_final_tool_call_is_not_executed(
    tmp_path: Path,
) -> None:
    """Turn 1 is cut by max_tokens after emitting two parseable calls: the
    first (output followed it — provably complete) executes; the LAST is
    refused with re-issue guidance. The model re-issues it on turn 2 (a
    clean turn) and it runs."""
    from lyre.adapter.llm_adapter import (
        ContentDelta,
        LyreContentBlock,
        LyreMessage,
        ToolUseComplete,
        TurnComplete,
    )

    from .fake_adapter import FakeAdapter

    adapter = FakeAdapter()
    adapter.push_turn([
        ToolUseComplete(id="t1", name="probe", input={"path": "/tmp/full"}),
        ToolUseComplete(id="t2", name="probe", input={"path": "/"}),
        TurnComplete(stop_reason="max_tokens"),
    ])
    adapter.push_turn([
        ToolUseComplete(id="t3", name="probe", input={"path": "/"}),
        TurnComplete(stop_reason="tool_use"),
    ])
    adapter.push_turn([
        ContentDelta(text="done"),
        TurnComplete(stop_reason="end_turn"),
    ])
    loop, transcript, executed = _probe_loop(tmp_path, adapter, "wakeup-s1")

    await loop.run(
        system_prompt="",
        initial_messages=[LyreMessage(
            role="user",
            content=[LyreContentBlock(type="text", text="go")],
        )],
    )
    transcript.close()

    # Turn 1: only the provably-complete first call ran. Turn 2: the
    # re-issue ran.
    assert executed == [{"path": "/tmp/full"}, {"path": "/"}]
    text = transcript.path.read_text()
    assert "was NOT executed" in text
    assert "re-issue it EXACTLY" in text


@pytest.mark.asyncio
async def test_clean_turn_executes_every_tool_call(tmp_path: Path) -> None:
    """No false positive: a turn that ends with stop_reason=tool_use runs
    every call, including the last."""
    from lyre.adapter.llm_adapter import (
        ContentDelta,
        LyreContentBlock,
        LyreMessage,
        ToolUseComplete,
        TurnComplete,
    )

    from .fake_adapter import FakeAdapter

    adapter = FakeAdapter()
    adapter.push_turn([
        ToolUseComplete(id="t1", name="probe", input={"n": 1}),
        ToolUseComplete(id="t2", name="probe", input={"n": 2}),
        TurnComplete(stop_reason="tool_use"),
    ])
    adapter.push_turn([
        ContentDelta(text="done"),
        TurnComplete(stop_reason="end_turn"),
    ])
    loop, transcript, executed = _probe_loop(tmp_path, adapter, "wakeup-s1c")

    await loop.run(
        system_prompt="",
        initial_messages=[LyreMessage(
            role="user",
            content=[LyreContentBlock(type="text", text="go")],
        )],
    )
    transcript.close()
    assert executed == [{"n": 1}, {"n": 2}]


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


# ---------------------------------------------------------------------------
# O3a: per-task TURN budget (max_turns) — config knob + build-site resolution
# ---------------------------------------------------------------------------


def test_max_turns_defaults_to_24_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """24 is the long-standing default the AgentLoop build site silently used
    when nothing passed max_turns. Lifting it into Config keeps that default
    while making it tunable."""
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LYRE_MAX_TURNS", raising=False)
    assert Config.from_env().max_turns == 24


def test_max_turns_reads_from_runtime_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LYRE_MAX_TURNS", raising=False)
    (tmp_path / "config.toml").write_text(
        '[owner]\nname = "o"\n\n[runtime]\nmax_turns = 40\n',
        encoding="utf-8",
    )
    assert Config.from_env().max_turns == 40


def test_max_turns_env_overrides_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LYRE_MAX_TURNS", "50")
    (tmp_path / "config.toml").write_text(
        '[owner]\nname = "o"\n\n[runtime]\nmax_turns = 40\n',
        encoding="utf-8",
    )
    assert Config.from_env().max_turns == 50


def test_max_turns_floors_at_1_for_zero_or_negative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misconfigured ``max_turns=0`` would wedge every wakeup at zero
    turns — clamp to a functional floor of 1."""
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LYRE_MAX_TURNS", raising=False)
    (tmp_path / "config.toml").write_text(
        '[owner]\nname = "o"\n\n[runtime]\nmax_turns = 0\n',
        encoding="utf-8",
    )
    assert Config.from_env().max_turns == 1


def test_max_turns_garbage_falls_back_to_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LYRE_MAX_TURNS", "not-a-number")
    assert Config.from_env().max_turns == 24


def _task(tier_overrides: dict[str, object] | None) -> Task:
    return Task(
        id="t1",
        persona_name="analyst",
        goal="g",
        acceptance="a",
        status="in_progress",
        tier_overrides=tier_overrides,
    )


def test_effective_max_turns_uses_per_task_override() -> None:
    """A dispatch that raised the budget (dispatch_task max_turns=) wins over
    the config default — this is the consumer that makes tier_overrides live."""
    assert _effective_max_turns(_task({"max_turns": 40}), 24) == 40


def test_effective_max_turns_falls_back_to_default_without_override() -> None:
    assert _effective_max_turns(_task(None), 24) == 24
    assert _effective_max_turns(_task({}), 24) == 24


@pytest.mark.parametrize("bad", [0, -1, "40", 3.5, True])
def test_effective_max_turns_ignores_malformed_override(bad: object) -> None:
    """A non-int / non-positive override (incl. bool, an int subclass) is
    ignored in favour of the default rather than wedging the wakeup."""
    assert _effective_max_turns(_task({"max_turns": bad}), 24) == 24


# ---------------------------------------------------------------------------
# R1: LLM transient-error retry budget (llm_max_retries) — config knob.
# ---------------------------------------------------------------------------


def test_llm_max_retries_defaults_to_2_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2 matches the provider SDK default, so the default behavior is unchanged
    — but now it's explicit and tunable."""
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LYRE_LLM_MAX_RETRIES", raising=False)
    assert Config.from_env().llm_max_retries == 2


def test_llm_max_retries_reads_from_runtime_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LYRE_LLM_MAX_RETRIES", raising=False)
    (tmp_path / "config.toml").write_text(
        '[owner]\nname = "o"\n\n[runtime]\nllm_max_retries = 4\n',
        encoding="utf-8",
    )
    assert Config.from_env().llm_max_retries == 4


def test_llm_max_retries_env_overrides_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LYRE_LLM_MAX_RETRIES", "6")
    (tmp_path / "config.toml").write_text(
        '[owner]\nname = "o"\n\n[runtime]\nllm_max_retries = 4\n',
        encoding="utf-8",
    )
    assert Config.from_env().llm_max_retries == 6


def test_llm_max_retries_floors_at_0_for_negative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative is meaningless; clamp to 0 (disables SDK retry)."""
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LYRE_LLM_MAX_RETRIES", "-3")
    assert Config.from_env().llm_max_retries == 0


# ---------------------------------------------------------------------------
# C + R2: singleton recovery bound + mid-stream failover budget — config knobs.
# ---------------------------------------------------------------------------


def test_singleton_recovery_max_default_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LYRE_SINGLETON_RECOVERY_MAX", raising=False)
    assert Config.from_env().singleton_recovery_max == 3
    monkeypatch.setenv("LYRE_SINGLETON_RECOVERY_MAX", "5")
    assert Config.from_env().singleton_recovery_max == 5
    monkeypatch.setenv("LYRE_SINGLETON_RECOVERY_MAX", "-1")
    assert Config.from_env().singleton_recovery_max == 0  # floor / disable


def test_midstream_max_retries_default_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LYRE_MIDSTREAM_MAX_RETRIES", raising=False)
    assert Config.from_env().midstream_max_retries == 1
    monkeypatch.setenv("LYRE_MIDSTREAM_MAX_RETRIES", "2")
    assert Config.from_env().midstream_max_retries == 2
    monkeypatch.setenv("LYRE_MIDSTREAM_MAX_RETRIES", "-5")
    assert Config.from_env().midstream_max_retries == 0  # floor / old fatal


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
