# Concepts

The vocabulary you need to read the codebase, write personas, and debug
weird behavior.

## The one-paragraph mental model

Lyre runs a small team of AI agents on your behalf. Each agent has its own
**mailbox** — a durable inbox/outbox in SQLite. Agents communicate **only**
by mail; they cannot call each other directly. Work happens in **wakeups**:
discrete sessions where one agent runs a tool-using LLM loop, reads its
mail, does things, sends replies, then idles. A **task** is a goal an
agent pursues across many wakeups. The **scheduler** decides when to wake
which agent. Every transition is persisted, so nothing is lost when you
kill the process.

## The five laws

These are settled. The rest of the system is built to honor them.

1. **Provider neutrality.** No code outside `src/lyre/adapter/` knows
   whether it's talking to Anthropic, DeepSeek, OpenAI, or a self-hosted
   vLLM. Adding a new provider is one adapter + one registry entry.

2. **Lyre is the gateway.** Anything that crosses the agent / outside-world
   boundary goes through a Lyre tool. Mail send/receive, file writes,
   shell commands, GitHub calls — all of them. Agents have no sidechannel.

3. **Kill-test as truth.** At any instant, killing any process must leave
   the system recoverable. This drives the outbox pattern, the lease
   model on tasks, the per-message read state, and the durable transcript.

4. **Persistence in three tiers.**
   - **Local-hot** (task-scoped): `task.checkpoint`, dropped when task ends
   - **Global** (cross-task, owner-scoped): `~/.lyre/` markdown files
     (`user.md`, `personas/`, `skills/`, `memory/facts/`, agent notes)
   - **Cold** (audit): transcripts in `~/.lyre/object_store/`,
     append-only, never read back into runtime

5. **Mailbox is the only communication primitive.** Owner→agent,
   agent→agent, future scheduled messages — all the same primitive.
   The dashboard, the CLI, and the agents themselves are all just
   different clients of the same mailbox layer.

## Core nouns

### Agent vs. Persona

These are **orthogonal**. Most multi-agent frameworks conflate them; Lyre
keeps them apart.

- **Persona** = the role definition. A markdown file at
  `src/lyre/personas/<name>.md` with frontmatter (allowed tools, model
  preference, kind) and a system-prompt body. Static.
- **Agent** = a running instance of a persona. Has an ID, a mailbox, a
  notes file, a task queue, transcripts. Dynamic. **One persona can have
  many agent instances** running in parallel.

Example: persona `worker-maintainer` is a single markdown file. The
dispatcher might `create_agent(persona="worker-maintainer")` three times,
producing agents `worker-maintainer/1`, `worker-maintainer/2`,
`worker-maintainer/3` (spawned agent ids are `persona/name`; pass `name`
to pick something more meaningful) — each with its own mailbox, working
on different PRs simultaneously.

When you `mailbox_send` or `dispatch_task`, the target is **always** an
`agent_id`, never a persona name.

### Mailbox

The only communication primitive. A `mailbox_messages` table row has:
sender, recipient, urgency (blocker/high/normal/low), title, body,
delivered_at, **read_at** (NULL = unread; set when the recipient calls
`mailbox_read`).

Key operations agents can perform:

- `mailbox_read()` — fetches unread mail (listing only — id, sender,
  urgency, title, body_chars) AND auto-marks them read
- `mailbox_get_message(msg_id)` — fetch the full body of one message
- `mailbox_read(box="sent")` — see what *you* sent (for cross-wakeup
  recall, since the in-memory conversation is discarded)
- `mailbox_send(to, body, title, ...)` — write to anyone, including
  yourself; supports broadcast (`to=[a, b, c]`), reply (`reply_to=N`),
  forward (`forward_msg_id=N`), and future delivery (`deliver_in="30m"`)
- `mark_read(msg_id)` — dismiss without replying

Mail goes through an **outbox** table on the send side. An async
dispatcher moves outbox rows into recipient mailboxes with idempotent
delivery (the `external_id` field deduplicates). This is what makes mail
at-least-once even across process death.

### Wakeup

A wakeup is one execution of the agent loop for one (agent, task) pair.
It owns a temporary lease on the task, runs the LLM in a multi-turn
streaming loop, dispatches tool calls, and ends naturally when the
model produces a response with **no further tool_use blocks**.

A wakeup is *stateless across wakeups*. The messages list (which
includes the model's thinking, text, and tool exchange) is discarded
when the wakeup ends. **Cross-wakeup state lives in:**

- The mailbox (durable; readable via `mailbox_read(box="sent")` and
  `include_read=True`)
- The scratchpad at `~/.lyre/memory/scratchpad/<flat-id>.md` — short-term
  working memory the model curates via the `update_scratchpad` tool
- The notes file at `~/.lyre/memory/facts/agent-<flat-id>-notes.md`
  (pre-created per agent, `/` in spawned ids flattened to `-`; agent
  writes via `shell_exec` or `python_exec`, and the runtime appends an
  auto-summary log here after each wakeup)
- The task's checkpoint (set via `report_progress`; used for crash
  recovery, NOT visible to anyone else)

The wakeup mechanics are described in detail in
[writing-personas.md](./writing-personas.md#mental-model-the-agent-must-have).

### Task

A unit of work an agent is pursuing. Lifecycle states:

- `pending` — queued, no agent has it yet
- `in_progress` — an agent has the lease and is running a wakeup
- `needs_input` — reserved park state, **dormant today**: nothing in the
  runtime parks a task, and the scheduler phase that would resume parked
  tasks (Phase 0.7) is inert. The runtime has no blocking primitive —
  agents that delegate work end their wakeup and rely on
  auto-wake-on-mail when children mail back
- `completed` / `failed` / `cancelled` — terminal

Tasks have an optional `parent_task_id` (set when one agent
`dispatch_task`s another). It records lineage only — there is **no
parent-resume mechanism**. When a child task terminates, the runtime
mails the result back (a `task_terminated` system mail, or a fan-in
"ready" mail when the children were dispatched as a fan-in group), and
the parent agent is woken by the ordinary unread-mail auto-wake.

Tasks have a `checkpoint` (JSON blob) the agent can update via
`report_progress` so a crashed wakeup can resume in the next one.

### Scheduler

The bottom-half of the runtime. Each tick (default 1s) runs a ladder of
phases (the gaps in the numbering are historical — there is no Phase 1):

- **Phase -1**: deliver due `scheduled_mail` rows as real mailbox
  messages (future-mail and recurring-mail mechanism)
- **Phase 0.5**: resolve fan-in barriers — count delivered result-mails
  for each open group and, on quorum/deadline, mail the coordinator a
  "ready" notification (which Phase 0 then picks up)
- **Phase 0**: scan agents for unread mail without an in-flight task;
  auto-dispatch a "check your inbox" task to wake them up
- **Phase 0.7**: resume parked `needs_input` tasks — *dormant*: nothing
  parks a task today
- **Phase 0.8**: reap ephemeral spawned agents whose work is done,
  applying their restart policies
- **Phase 2**: recover tasks whose lease expired (process killed
  mid-wakeup) by re-running them
- **Phase 3**: claim pending tasks (bootstrap singletons first, with a
  reserved slot) and run wakeups, one in-flight wakeup per agent
- **Phase 4**: throttled DB maintenance (retention prune + WAL checkpoint)

There is no parent-resume phase: all cross-agent synchronisation is
mail-driven (children mail results back; the parent auto-wakes on unread
mail). The scheduler is single-threaded async; under `lyre serve` each
wakeup runs in a `lyre run-task` subprocess by default, several in
flight concurrently. SQLite WAL mode handles the per-write serialization.

### Auto-compaction

When a wakeup's accumulated input tokens cross **70%** of the model's
context window (configurable via `LYRE_COMPACT_THRESHOLD`), the runtime
compacts the messages list mid-flight:

- Preserves all `mailbox_get_message` results as synthetic user messages
  (owner / peer content survives verbatim — they're quasi user input
  in Lyre, unlike most frameworks where user input is in `role="user"`)
- Preserves all `mailbox_send` calls as synthetic assistant messages
  (the agent's own commitments aren't lost)
- Summarizes everything else (shell output, file reads, etc.) via one
  same-model LLM call
- Keeps the last 3 turn-pairs intact (preserves thinking-block-to-
  tool-use binding for the next API call)

Per-wakeup metrics — peak context tokens, compaction count — are
recorded on the `wakeups` row and surfaced in the dashboard.

### Identity preamble

Every wakeup's system prompt starts with a runtime-generated **identity
preamble** that explains the same mechanical facts to every agent:

- Who they are (agent_id, persona name)
- How wakeups end (when you stop calling tools — there's no `end_turn` tool)
- That `mailbox_send` doesn't yield the wakeup
- The ack-and-stop anti-pattern ("I'll look into it" → stop = a lie)
- Where their notes file lives
- The legitimate "do this later" paths (real `dispatch_task`, future-mail)

The preamble is byte-identical across wakeups for the same agent, which
matters for prompt-cache hits on Anthropic and DeepSeek.

## The lifecycle of one piece of work

To make this concrete, here's what happens when you `lyre send dispatcher
"please count the .py files in the project"`:

1. CLI writes a row to `mailbox_messages` (recipient=`dispatcher`,
   sender=`owner`, body=...).
2. Scheduler's Phase 0 sees `dispatcher` has unread mail and no active
   task. It creates an auto-wake task with goal "Check your inbox..."
   and dispatches it.
3. AgentLoop starts. It assembles the system prompt (identity preamble
   + persona body + global memory index + project AGENTS.md walk +
   skills XML), constructs the initial user message (task goal), and
   begins streaming from the chosen LLM.
4. Model emits tool calls: `mailbox_read()` (gets your message in a
   listing), `mailbox_get_message(msg_id=N)` (fetches your full body),
   `shell_exec(argv=["find", "src", "-name", "*.py"])`, etc. The loop
   continues for as long as the model keeps calling tools.
5. Model emits `mailbox_send(to="owner", body="61 files")`.
6. Model produces one more response with no tool_use blocks → loop
   exits. Wakeup ends, `wakeups` row finalized with metering
   (tokens, wall_ms, context peak, compaction count).
7. Outbox dispatcher delivers the reply into `owner`'s mailbox.
8. `lyre mailbox owner --unread-only` shows it.

If the task was bigger and `dispatcher` decided to delegate — e.g.,
"have a worker-maintainer do this" — step 4 instead calls
`dispatch_task(agent="worker-maintainer/1", ...)` and then **just
stops calling tools**. The wakeup ends; `dispatcher`'s task completes.
The worker runs its own wakeup; when it finishes, it `mailbox_send`s
back to `dispatcher`. Auto-wake-on-mail starts a fresh wakeup of
`dispatcher` to read the worker's reply and forward findings to you.
Two wakeups, one mail thread — the runtime has no blocking "wait for
children" primitive, all synchronisation is mail-driven. (For
many-children patterns there's a fan-in barrier: `fan_in_open` a group,
dispatch the legs, and the scheduler mails the coordinator one "ready"
notification when enough result-mails have arrived.)

This is what "long-running" means in Lyre. The conversation isn't one
session — it's a chain of wakeups, possibly spread over hours or days,
all backed by durable state.

## Where to read more

- **Internal design** (Chinese, `docs/design/`): `FOUNDATION.md` for the
  five laws in depth, `AGENT_CONTRACT.md` for the agent interface,
  `TRANSACTION_BOUNDARIES.md` for the commit-point and outbox patterns,
  `PERSISTENCE_SCHEMA.md` for the full schema.
- **Code** (Python, `src/lyre/`): `runtime/agent_loop.py` for the
  wakeup loop, `scheduler/scheduler.py` for scheduling phases,
  `adapter/` for provider integrations, `runtime/compact.py` for
  context compaction, `runtime/context.py` for prompt assembly.
