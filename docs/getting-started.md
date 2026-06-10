# Getting Started

Five minutes from `git clone` to seeing your first agent reply.

## 1. Install

Lyre uses [uv](https://github.com/astral-sh/uv) as its package manager.

```bash
git clone https://github.com/<your-org>/lyre.git
cd lyre
uv sync
```

This pulls Python 3.11+, the Anthropic / OpenAI SDKs, FastAPI for the
dashboard, and SQLite-based persistence. No Docker required.

## 2. Run the onboard wizard

```bash
uv run lyre onboard
```

The wizard asks for:

- Owner name + email (defaults from `git config`)
- A starter provider (Anthropic / OpenAI / DeepSeek / OpenRouter / skip)
- Where to put the API key — your shell env, or `~/.lyre/.env` (chmod 600)

Then it writes:

- `~/.lyre/config.toml` — your identity + chosen provider/model defaults
- `~/.lyre/user.md` — a starter template for your identity & preferences (agents read this every wakeup)
- `~/.lyre/personas/<name>/identity.md` — shipped personas copied here as the SSOT (edit / rename / delete freely)
- `~/.lyre/lyre.db` — SQLite for tasks / wakeups / mailbox
- `~/.lyre/memory/` — markdown filesystem the agents write to
- `~/.lyre/memory/facts/agent-<id>-notes.md` — one per-agent notebook
  file for each seeded agent (e.g. `agent-dispatcher-notes.md`)
- Seeded agents: `owner` (you, addressable but not LLM-driven),
  `dispatcher` (the orchestrator), plus one starter `analyst-1` and
  `reviewer-1`

Re-running `lyre onboard` is safe — each overwrite is gated by a
confirmation prompt. For scripted / headless setup, hand-edit
`~/.lyre/config.toml` + `~/.lyre/.env` yourself and skip the wizard.

## 3. Start the runtime

```bash
uv run lyre serve
```

This boots three things in one process:

1. **Scheduler** — wakes agents when mail arrives or when a task is due
2. **Outbox dispatcher** — moves mail from the send-side `outbox` table
   into recipient mailboxes (the at-least-once delivery layer)
3. **Dashboard** — web UI at <http://127.0.0.1:8765>

Leave this running in a terminal. Press `Ctrl+C` to stop.

## 4. Talk to your team

In another terminal:

```bash
uv run lyre send dispatcher "Hi dispatcher, please reply with 'pong' and tell me what model you're running on."
```

Within a few seconds you'll see in the original terminal:

- The scheduler dispatching a "check inbox" task to `dispatcher`
- An LLM call going out, streaming back
- `dispatcher` calling `mailbox_send` to reply to you

Then check what came back:

```bash
uv run lyre mailbox owner --unread-only
```

You should see `dispatcher`'s reply.

## 5. See it in the dashboard

Open <http://127.0.0.1:8765> in a browser. You'll see:

- The **Activity** tab — a chat-bubble timeline of everything that just
  happened, including the model's thinking (rendered with a brain badge),
  every tool call, and the mail it sent
- The **Agents** tab — drill into `dispatcher` to see its full transcript
- The **Mail** tab — mailbox traffic, including your own inbox

## What just happened

A lot, actually. Walking through one cycle:

1. `lyre send dispatcher "..."` wrote a row to the `mailbox_messages` table
   (recipient=`dispatcher`).
2. The scheduler's Phase 0 noticed `dispatcher` had unread mail and no
   in-flight task, so it auto-created a task with goal *"Check your
   inbox..."* and dispatched it.
3. The agent loop started a new wakeup, loaded `dispatcher`'s persona prompt
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
uv run lyre audit --latest --persona dispatcher
```

## Next steps

- Read [concepts.md](./concepts.md) to understand the runtime's mental
  model — agents vs. personas, what a wakeup is, why mailbox-only.
- Read [writing-personas.md](./writing-personas.md) to add your own
  agent types beyond `dispatcher` + the bundled workers.
- Read [cli-reference.md](./cli-reference.md) for the full command
  reference and debug recipes.
