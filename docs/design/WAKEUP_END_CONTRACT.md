# Wakeup End Contract

**Status:** Draft — spec, not yet implemented.
**Scope:** Make every wakeup terminate via an explicit declaration. The
runtime stops inferring intent from "the model produced no tool_use."
Net tool surface change is **zero**: `end_wakeup` replaces
`report_progress`, which an audit (§3) found to be vestigial.

## 1. Motivation

Today the runtime decides a wakeup is over when the LLM returns a
response with no `tool_use` blocks. The `AgentLoopResult.status` is
then derived from observable facts (`silent_close` / `completed` /
`needs_continuation`) and mapped to a coarse `tasks.status`
(`completed` / `failed`). The agent itself never *declares* why the
wakeup ended.

This is the kill-test law's blind spot on the live-runtime side:
SIGKILL is recoverable because leases expire, but **a wakeup that
returns cleanly without doing real work is indistinguishable from one
that succeeded**. Six failure modes collapse onto two terminal
statuses:

| Observed behaviour | Today's `tasks.status` | Actual intent |
|---|---|---|
| "I'll get to it" then end_turn | `completed` | Nothing was done — *ack-and-stop antipattern* |
| `dispatch_task` then end_turn | `completed` (silent_close) | Should be waiting for subtask |
| `mailbox_send` to owner then end_turn | `completed` | Should be waiting for reply |
| Empty model response | `completed` | Provider hiccup / refusal |
| `max_turns` reached | `failed` (needs_continuation) | Possibly retryable, possibly wedged |
| Tool raised | `failed` | No structured reason, no retry hint |

The operator looking at the dashboard sees task=completed and assumes
the work landed. PR #28 (`task_terminated` mail proposal) cannot push
useful failure notifications downstream because there is no structured
reason to forward.

## 2. Proposed Contract

> **Every wakeup MUST terminate by calling `end_wakeup(...)` as its
> last tool call.** The tool's `status` field declares the wakeup
> outcome; the runtime maps it onto `tasks.status` and
> `wakeups.end_status` deterministically. No more inference from
> syntactic side effects.

This is the LLM analogue of an OTP `gen_server` callback's return
tuple (`{:reply, R, S}` / `{:noreply, S}` / `{:stop, Reason, S}`).
The wakeup is one callback invocation; `end_wakeup` is its return
statement.

## 3. Tool Surface — `end_wakeup` replaces `report_progress`

This spec does NOT add net tool surface. It removes `report_progress`
and `tasks.checkpoint` and introduces `end_wakeup`.

### 3a. Audit: why `report_progress` is redundant

`report_progress(checkpoint, note)` writes a free-form JSON blob into
`tasks.checkpoint`. The whole codebase has exactly two consumers of
that column:

- `runtime/context.py` injects `tasks.checkpoint` into the next
  wakeup's task description as `【续做 checkpoint】\n{checkpoint}\n`.
  Nothing parses the JSON; it is `str(obj)`-formatted and concatenated
  into the prompt.
- `query_task_status` exposes it via the read-only API.

Compared to `update_scratchpad` (which writes
`memory/scratchpad/<flat-id>.md`), the only unique behaviour
`report_progress` offers is *auto-injection into the task description
of the next wakeup of the same task*. Personas already instruct the
agent to read its scratchpad at every wakeup start, so this
convenience adds little — and at the cost of carrying a second
state-continuity surface with different scope (per-task vs per-agent),
different durability (dropped with the task vs persistent across
tasks), and no size cap / curation pattern.

The "checkpoint is structured, scratchpad is free-form" framing is
illusory: nothing inspects the JSON, the LLM reads markdown equally
well, and the JSON wrapper just costs tokens.

The audit conclusion: `report_progress` is vestigial. Folding the
crash-recovery continuity story into `update_scratchpad` loses
nothing real and removes one tool plus one DB column.

### 3b. `end_wakeup` tool

```jsonc
{
  "name": "end_wakeup",
  "description": "Declare the outcome of this wakeup. Must be the LAST tool call. ...",
  "input_schema": {
    "type": "object",
    "properties": {
      "status": {
        "type": "string",
        "enum": ["done", "in_progress", "awaiting", "failed"]
      },
      "summary": {
        "type": "string",
        "description": "One- or two-sentence human-readable wrap-up. Becomes the wakeup's end-of-life note in transcripts and the failure_report field."
      },
      "awaiting_on": {
        "type": "string",
        "enum": ["mail", "subtask", "time", "human_decision"],
        "description": "Required iff status='awaiting'. What the next wakeup is gated on."
      },
      "awaiting_ref": {
        "type": "string",
        "description": "Optional. Identifier the scheduler can use to resume precisely (sender agent id / subtask id / ISO timestamp)."
      },
      "failure_reason": {
        "type": "string",
        "enum": [
          "loop_exhausted",
          "tool_error",
          "provider_error",
          "precondition_failed",
          "dependency_failed",
          "cancelled_by_owner",
          "cancelled_by_parent",
          "policy_violation",
          "silent_close"
        ],
        "description": "Required iff status='failed'. Categorises the failure so supervisors / task_terminated mail can react."
      },
      "recoverable": {
        "type": "boolean",
        "description": "Only meaningful with status='failed'. True hints that retry might succeed (e.g. transient provider 5xx); false hints the same wedged state will recur (e.g. precondition_failed, policy_violation)."
      }
    },
    "required": ["status", "summary"]
  }
}
```

### Status values

| `status` | Meaning | `tasks.status` after | `wakeups.end_status` |
|---|---|---|---|
| `done` | Task goal met. No further wakeups needed. | `completed` | `completed` |
| `in_progress` | Agent deliberately yields. Wants another wakeup soon. | `in_progress` (kept) | `yielded` |
| `awaiting` | Blocked on an external trigger. | `needs_input` | `awaiting_<on>` |
| `failed` | Cannot make progress. | `failed` | `failed_<reason>` |

### Awaiting kinds

| `awaiting_on` | Resume trigger | Typical use |
|---|---|---|
| `mail` | New unread mail in agent's mailbox | Sent owner a question, waiting for answer |
| `subtask` | A specific child task reaches terminal state | Dispatched work to a worker, waiting for result |
| `time` | Scheduled timestamp passes | Polling cadence, deferred action |
| `human_decision` | Owner explicitly resumes via CLI/dashboard | Sensitive decisions; no auto-resume |

`awaiting_ref` is optional but strongly encouraged for `subtask`
(carries the child task id so the scheduler resumes precisely instead
of scanning `parent_task_id` relations).

### Failure reasons

A closed enum so downstream consumers (`task_terminated` mail,
supervisor personas) can pattern-match:

| `failure_reason` | Description | Typical `recoverable` |
|---|---|---|
| `loop_exhausted` | Hit `max_turns`. Often a tool-call loop or malformed args. | `true` if the underlying provider/tool issue is transient |
| `tool_error` | A tool raised; the agent saw the error and decided to bail. | Depends on tool — agent supplies the judgement |
| `provider_error` | LLM API 5xx / quota / no candidates after fallback. | Almost always `true` |
| `precondition_failed` | Required input is missing or invalid (file not found, dep unavailable). | Usually `false` — re-running won't make the input appear |
| `dependency_failed` | Subtask the agent was waiting on returned `failed`. | `false` (cascade) |
| `cancelled_by_owner` | Agent saw a cancel mail from owner / dashboard. | n/a |
| `cancelled_by_parent` | Parent task issued cancellation. | n/a |
| `policy_violation` | Agent attempted an action its behavior contract forbade (future-work: ties into the `gen_*` behavior system). | `false` |
| `silent_close` | Runtime-set ONLY. Agent failed to declare even after the nudge. | `false` |

`silent_close` is the runtime's fallback when enforcement (§6) cannot
extract a declaration. Agents should never set it themselves.

## 4. State Transition Table

`end_wakeup` is the **only** path that writes `tasks.status` /
`wakeups.end_status` at end-of-wakeup. The scheduler's
`_run_task_inline` no longer derives status from `AgentLoopResult` —
it reads the declaration from the loop state.

```
end_wakeup status → tasks.status            wakeups.end_status
─────────────────────────────────────────────────────────────────
done                completed                completed
in_progress         in_progress              yielded
awaiting (mail)     needs_input              awaiting_mail
awaiting (subtask)  needs_input              awaiting_subtask
awaiting (time)     needs_input              awaiting_time
awaiting (human)    needs_input              awaiting_human
failed              failed                   failed_<reason>
```

Exception paths (the runtime never reaches `end_wakeup` because
something blew up earlier):

```
unhandled Python exception in agent_loop          → failed_runtime_exception
SIGKILL / process death                            → no commit; lease recovery
scheduler-side cancel (kill switch)                → cancelled_runtime
no end_wakeup declared after nudge (§6)           → failed_silent_close
```

`failed_runtime_exception` and `failed_silent_close` are runtime-only
end_statuses; they are NOT exposed as values agents can pass.

## 5. Terminal-only semantics

`end_wakeup` is terminal: calling it ends the wakeup. The runtime
stops processing further model output after the tool result returns.
If the model attempts to produce more `tool_use` blocks after
`end_wakeup`, they are dropped with a transcript warning
(`wakeup_post_end_tool_calls_ignored`).

The "where do I park work-in-progress so a crash doesn't lose it"
concern is now served by `update_scratchpad` alone. Personas already
treat the scratchpad as the agent's working-memory canvas; task-scoped
continuity belongs there under an `## Active task: <id> — <goal>`
heading the agent maintains itself. No separate `tasks.checkpoint`
state is needed (see §3a audit).

## 6. Runtime Enforcement

Three layers, increasing strictness:

### 6a. Identity preamble (soft, prompt)

The runtime-generated identity preamble gains a section explaining
the contract:

```text
END OF WAKEUP

You MUST end every wakeup by calling end_wakeup(...) as your last tool
call. Until you do, the runtime cannot tell whether your work
succeeded, is waiting on something, or failed. The four statuses are:

  - done:        Task is complete; no further wakeups needed.
  - awaiting:    Blocked on mail / subtask / time / human decision.
                 Specify awaiting_on (and awaiting_ref if you can).
  - in_progress: You deliberately yielded; another wakeup will resume.
  - failed:      Cannot make progress. Specify failure_reason.

The "ack-and-stop antipattern" is now a runtime error: stopping
without calling end_wakeup will be recorded as failed_silent_close
and surfaced to the supervisor.
```

The current ack-and-stop warning gets replaced by this section.

### 6b. Single nudge (runtime, one extra turn)

If `agent_loop` detects a turn that produced no `tool_use` AND the
last tool call wasn't `end_wakeup`:

1. Append a synthetic user message:
   > Your last response had no `end_wakeup` call. The wakeup cannot
   > terminate cleanly without one. Call `end_wakeup` now with the
   > status that best describes your situation: done / in_progress /
   > awaiting / failed.
2. Run **one more turn** with the same tool set.
3. If that turn produces a valid `end_wakeup` call: accept it.
4. If it produces another no-tool response, or a `tool_use` that
   isn't `end_wakeup`: go to 6c.

The nudge costs one short turn (~50–200 tokens) but only fires when
the agent already silently ended. Net cost is small; it catches
ack-and-stop reliably.

### 6c. Hard fallback (runtime, no further LLM call)

If the nudge fails, the runtime synthesises:

```python
end_wakeup(
    status="failed",
    summary="(auto) wakeup ended without declaring an outcome",
    failure_reason="silent_close",
    recoverable=False,
)
```

Logs `wakeup_silent_close_forced` with the wakeup id, last few tool
calls, and final text. The wakeup is recorded as `failed_silent_close`
so it surfaces on the dashboard and in any downstream supervisor's
mailbox via `task_terminated`.

## 7. Migration

### Removing `report_progress` and `tasks.checkpoint`

- Delete `REPORT_PROGRESS` tool + `_report_progress` handler in
  `runtime/tools/progress.py`. The file becomes
  `report_side_effect`-only (consider renaming the module to
  `side_effect.py`; minor cleanup).
- Delete the `update_checkpoint` DAO method on `TaskRepository`
  (protocol + Sqlite impl) and any callers.
- Delete the `tasks.checkpoint` column from `migrations/0001_initial.sql`.
  Per project policy ("edit 0001_initial.sql in place"), owners nuke
  their local DB.
- Delete the `checkpoint` field from `Task` / `TaskSpec` Pydantic
  models.
- Delete the `tasks.checkpoint` injection block in
  `runtime/context.py:547-548`.
- Drop the `checkpoint` field from `query_task_status` response.
- Persona edits in `src/lyre/personas/{analyst,worker-maintainer,dispatcher}.md`:
  references to `report_progress(checkpoint={...})` are replaced with
  `update_scratchpad(...)` (under an `## Active task` heading) and the
  new `end_wakeup` instructions from §6a.

### Existing personas

Every shipped persona (`src/lyre/personas/*.md`) needs a paragraph
added to its role description noting the contract and giving one
representative example for that persona kind:

- **worker** personas: example using `end_wakeup(status='done', ...)`
  and `end_wakeup(status='failed', failure_reason='tool_error', ...)`
- **orchestrator / dispatcher** personas: example using
  `end_wakeup(status='awaiting', awaiting_on='subtask', awaiting_ref=...)`
- **analyst** personas: example using `done` and `awaiting_on='mail'`

User-edited copies (`~/.lyre/personas/<name>/identity.md`) are NOT
overwritten on upgrade — owner must manually merge or accept the
shipped version. The identity-preamble injection (§6a) is automatic
and applies regardless of persona edits.

### Existing tasks / wakeups

Schema change is the `tasks.checkpoint` column removal (one line in
`0001_initial.sql`). Owners nuke local DB per project policy. No
data migration is provided; the column's content was per-task
crash-recovery state which is by definition transient.

`wakeups.end_status` remains `TEXT` with no CHECK and accepts the
new values (`yielded`, `awaiting_<on>`, `failed_<reason>`,
`failed_silent_close`, `failed_runtime_exception`) without schema
change.

The dashboard's `_severity_for_wakeup` map gains entries for the new
values:

```python
"yielded"            → "info"   (deliberate yield is normal)
"awaiting_*"         → "info"
"failed_*"           → "alert"  (any failure)
"failed_silent_close"→ "alert"  (specifically operator-visible)
"completed"          → "ok"     (unchanged)
```

### Test suite

Two sweep passes:

1. `tests/fake_adapter.py`-based tests that previously ended their
   fake-turn sequences with `TurnComplete(stop_reason='end_turn')`
   and no `tool_use` must add a final
   `ToolUseComplete(name='end_wakeup', ...)` before the terminal
   `TurnComplete`. The number of affected tests is bounded.
2. Existing `report_progress` tests (`test_tools.py`, `test_persistence.py`,
   relevant chaos cases) get removed or rewritten against
   `update_scratchpad` if the underlying assertion was about
   continuity rather than the specific tool.

## 8. Implementation Sketch

Approximate file-by-file impact:

| File | Change |
|---|---|
| `src/lyre/runtime/tools/progress.py` | Delete `REPORT_PROGRESS` + handler. Add `END_WAKEUP` tool + handler. (Consider renaming module to `side_effect.py`; module-level cleanup is a nicety, not a requirement.) |
| `src/lyre/runtime/tools/builtin.py` | Remove `REPORT_PROGRESS` registration; add `END_WAKEUP`. |
| `src/lyre/persistence/models.py` | Drop `checkpoint` field from `Task` / `TaskSpec`. |
| `src/lyre/persistence/repositories.py` | Drop `update_checkpoint` method from `TaskRepository` protocol. |
| `src/lyre/persistence/sqlite_impl.py` | Drop `update_checkpoint` impl. Drop `checkpoint` from `_row_to_task` deserialiser. |
| `migrations/0001_initial.sql` | Remove the `checkpoint TEXT` column from the `tasks` table. |
| `src/lyre/runtime/tools/tasks.py` | Drop `checkpoint` from `query_task_status` response. |
| `src/lyre/runtime/context.py` | Drop the `task.checkpoint` injection block. Inject the §6a identity-preamble paragraph. Remove the now-redundant ack-and-stop warning text. |
| `src/lyre/runtime/agent_loop.py` | Detect `end_wakeup` call in tool dispatch; set loop-state flag; stop accepting more output. Add nudge logic + hard fallback. Change `AgentLoopResult` to carry the declared status + reason + awaiting info. |
| `src/lyre/scheduler/scheduler.py` | Replace `_wakeup_status_to_task_status` map: read directly from the declaration. Map awaiting kinds to `tasks.status='needs_input'` + remember `awaiting_on/ref` (see Q1 in §10). |
| `src/lyre/dashboard/activity.py` | Severity map updates per §7. |
| `src/lyre/personas/{analyst,worker-maintainer,dispatcher}.md` | Replace `report_progress` references with `update_scratchpad`. Add the per-persona migration paragraph for `end_wakeup`. Update `allowed_lyre_tools` frontmatter (drop `report_progress`, add `end_wakeup`). |
| `tests/test_agent_loop_*.py`, `tests/test_scheduler.py`, ... | Append `end_wakeup` to fake-turn sequences. New tests for the contract itself (§9). |
| `tests/test_tools.py`, `tests/test_persistence.py`, `tests/test_chaos_kill_points.py` | Remove `report_progress` / `checkpoint` tests; rewrite continuity-oriented assertions against `update_scratchpad` where applicable. |

Estimated diff: +500 / -300 across runtime + persistence; +80 / -40
across personas; tests net out near zero (new contract tests offset
removed checkpoint tests).

## 9. Test Plan

New tests (all offline, via `tests/fake_adapter.py`):

1. **Happy path: `end_wakeup(status='done')`** → `tasks.status='completed'`, `wakeups.end_status='completed'`.
2. **Yield: `end_wakeup(status='in_progress')`** → task stays `in_progress`, scheduler picks it up next tick.
3. **Awaiting subtask: `end_wakeup(status='awaiting', awaiting_on='subtask', awaiting_ref=child_id)`** → task `needs_input`, scheduler resumes only when `child_id` reaches terminal state.
4. **Awaiting mail** → `needs_input`, resumes when new mail arrives.
5. **Failed with recoverable: `end_wakeup(status='failed', failure_reason='provider_error', recoverable=True)`** → task `failed`, mail body to supervisor (when §11/PR #28 lands) carries the structured reason.
6. **Failed with non-recoverable: `end_wakeup(status='failed', failure_reason='precondition_failed', recoverable=False)`** → as above, recoverable hint propagated.
7. **Nudge succeeds**: fake turn produces no tool_use → runtime nudges → next fake turn produces `end_wakeup(status='done')` → wakeup accepts the declaration.
8. **Nudge fails, hard fallback**: fake turn produces no tool_use → nudge → another no-tool turn → runtime force-writes `failed_silent_close`.
9. **Trailing tool calls after `end_wakeup` are dropped**: fake turn calls `end_wakeup(...)` then another `mailbox_send` — the mailbox_send is logged as a warning, not executed.
10. **Schema validation**: `end_wakeup(status='awaiting')` without `awaiting_on` → `ToolError`; `end_wakeup(status='failed')` without `failure_reason` → `ToolError`.
11. **Identity preamble contains the §6a paragraph** — regression test ensuring future preamble edits don't drop it.
12. **No `report_progress` / `checkpoint` symbol leaks**: a grep-based regression test (or `ruff`/`mypy` natural coverage from the removals) confirming no production code references the deleted surface.

## 10. Open Questions

These deserve a follow-up discussion before implementation:

**Q1. Where does `awaiting_ref` live?**
The wakeup row's `end_status` is a single TEXT field. Stashing
structured awaiting info there (e.g. `awaiting_subtask:019e...`) is
greppable but ugly. Cleaner option: add a `wakeups.awaiting_ref TEXT`
column (and matching `awaiting_on TEXT` for symmetry with the enum).
That is a 0001_initial.sql edit; per project policy the owner nukes
local DB, so safe to do — but worth confirming we want a schema bump
for this vs encoding inline.

**Q2. Should `in_progress` yield require a reason?**
Right now `status='in_progress'` accepts only `summary`. Should it
also require a `yield_reason` enum (`context_full`, `time_budget`,
`fairness`, ...)? Probably overkill for v1 — `summary` is enough for
diagnosis. Revisit if dashboard analytics want to slice yields.

**Q3. Does `end_wakeup` count toward `max_turns`?**
Each turn ends with the model returning content; `end_wakeup` is
content. So yes, it consumes one turn slot. If the agent is at
`max_turns - 1`, it can still terminate cleanly because `end_wakeup`
needs only one turn. If it's already past `max_turns` mid-thought,
that's `loop_exhausted` — the runtime injects the hard fallback. This
is the right behaviour but should be tested (test §9.8 covers it).

**Q4. How does `report_side_effect` interact?**
`report_side_effect` is fine to call mid-wakeup before `end_wakeup`.
It's neither terminal nor a checkpoint; it's a notification. No
change needed.

**Q5. What about wakeups that legitimately do nothing?**
Auto-wake-on-mail Phase 0 may rouse an agent for mail that turns out
to be `low` urgency / informational. The agent might decide "ack
read, nothing to do." The right declaration is
`end_wakeup(status='done', summary='read informational mail, no action')`.
The identity preamble should mention this as a valid pattern so
agents don't feel forced to do something just to satisfy `status=done`.

## 11. Non-goals

Out of scope for this spec; tracked separately:

- **`task_terminated` mail delivery** — surfaced in the Erlang-model
  brainstorm. Will be a follow-up PR built on top of the structured
  reasons defined here.
- **Supervisor persona kind** — also a follow-up. The reason enum is
  the API contract; supervisor pattern-matches it.
- **Behavior contracts (`gen_worker`, `gen_orchestrator` …)** —
  policy_violation references them in the enum, but the behavior
  enforcement layer itself is future work.
- **Wakeup heartbeat** — `awaiting_on='time'` covers scheduled-poll
  agents; mid-wakeup wedge detection is a separate concern.
- **Atomic commit phase reordering** (`docs/design/TRANSACTION_BOUNDARIES.md`
  update for buffered scratchpad/notes) — separate spec, will use
  `end_wakeup`'s commit hook but its design is independent.
