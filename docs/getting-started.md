# Getting Started

Five minutes from `git clone` to seeing your first agent reply.

## 1. Install

Lyre uses [uv](https://github.com/astral-sh/uv) as its package manager.

```bash
git clone https://github.com/<your-org>/lyre.git
cd lyre
uv sync
```

This pulls Python 3.10+, the Anthropic / OpenAI SDKs, FastAPI for the
dashboard, and SQLite-based persistence. No Docker required.

## 2. Set an API key

Lyre's default model registry ships entries for Anthropic, DeepSeek, and
OpenAI. You need a key for **at least one** provider. DeepSeek is the
cheapest path for trying things out:

```bash
# Pick one — set whichever you have access to:
export ANTHROPIC_API_KEY=sk-ant-...
export DEEPSEEK_API_KEY=sk-...
export OPENAI_API_KEY=sk-...
```

You can also drop these into a `.env` file at the repo root; Lyre reads it
automatically.

## 3. Initialize state

```bash
uv run lyre init
```

This creates:

- `~/.lyre/lyre.db` — SQLite database for tasks / wakeups / mailbox
- `~/.lyre/memory/` — markdown filesystem the agents read and write
- `~/.lyre/memory/facts/agent-owner-notes.md` + `agent-leader-notes.md`
  — pre-created notebook files for the two bootstrap agents
- Two seeded agents: `owner` (you, addressable but not LLM-driven) and
  `leader` (the dispatcher persona)

You only need to run this once per machine.

## 4. Start the runtime

```bash
uv run lyre serve
```

This boots three things in one process:

1. **Scheduler** — wakes agents when mail arrives or when a task is due
2. **Outbox dispatcher** — moves mail from the send-side `outbox` table
   into recipient mailboxes (the at-least-once delivery layer)
3. **Dashboard** — web UI at <http://127.0.0.1:8765>

Leave this running in a terminal. Press `Ctrl+C` to stop.

## 5. Talk to your team

In another terminal:

```bash
uv run lyre send leader "Hi leader, please reply with 'pong' and tell me what model you're running on."
```

Within a few seconds you'll see in the original terminal:

- The scheduler dispatching a "check inbox" task to `leader`
- An LLM call going out, streaming back
- `leader` calling `mailbox_send` to reply to you

Then check what came back:

```bash
uv run lyre mailbox owner --unread-only
```

You should see `leader`'s reply.

## 6. See it in the dashboard

Open <http://127.0.0.1:8765> in a browser. You'll see:

- The **Activity** tab — a chat-bubble timeline of everything that just
  happened, including the model's thinking (rendered with a brain badge),
  every tool call, and the mail it sent
- The **Agents** tab — drill into `leader` to see its full transcript
- The **Inbox** tab — your own mailbox

## What just happened

A lot, actually. Walking through one cycle:

1. `lyre send leader "..."` wrote a row to the `mailbox_messages` table
   (recipient=`leader`).
2. The scheduler's Phase 0 noticed `leader` had unread mail and no
   in-flight task, so it auto-created a task with goal *"Check your
   inbox..."* and dispatched it.
3. The agent loop started a new wakeup, loaded `leader`'s persona prompt
   plus identity preamble, included the task goal as the initial user
   message, and called the LLM with the registered tools.
4. The model called `mailbox_read()` (which auto-marked the message as
   read), `mailbox_get_message(msg_id=N)` to fetch your body, possibly
   `read_memory()` to consult its notes file, then `mailbox_send(to="owner",
   body="pong, running on X")`.
5. The send went through the outbox and dispatcher into your mailbox.
6. The wakeup ended when the model produced a response with no further
   tool calls (the loop is **stop-reason-agnostic**; it ends when there's
   no more tool use, not when the model says "end_turn").

You can see the full trace via:

```bash
uv run lyre audit --latest --persona leader
```

## Next steps

- Read [concepts.md](./concepts.md) to understand the runtime's mental
  model — agents vs. personas, what a wakeup is, why mailbox-only.
- Read [writing-personas.md](./writing-personas.md) to add your own
  agent types beyond `leader` + the bundled workers.
- Read [cli-reference.md](./cli-reference.md) for the full command
  reference and debug recipes.
