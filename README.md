# Lyre

> A long-running personal multi-agent runtime. Agents persist across restarts,
> communicate only via durable mailboxes, and pursue work across many wakeups.

Lyre runs a small team of AI agents on your behalf. Each agent has its own
mailbox in SQLite. Agents communicate **only** by mail — they cannot call
each other directly. Work happens in **wakeups**: discrete sessions where
one agent runs a tool-using LLM loop, reads its mail, does things, sends
replies, then idles. A task is a goal an agent pursues across many wakeups,
backed by durable state.

Provider-neutral: Anthropic, DeepSeek (Anthropic-compat or OpenAI-compat),
OpenAI, OpenRouter, vLLM-served models — all behind the same `LLMAdapter`
interface. Switching providers is a YAML edit.

## What makes Lyre different

- **Long-running, not session-based.** A task isn't one prompt. It's a goal
  the runtime pursues across many model wakeups, surviving process restarts
  and machine reboots through SQLite-backed state.

- **Mailbox-only communication.** Owner → agent, agent → agent, and future
  scheduled messages all flow through the same persistent mailbox. The
  dashboard, the CLI, and the agents themselves are just three clients of
  one mailbox layer.

- **Personal, not organizational.** Single owner. No multi-tenant isolation,
  no RBAC, no compliance surface. Optimized for *your* workflow, not a team.

- **Kill-resistant by design.** At any instant, killing any process leaves
  the system recoverable. This drives the outbox pattern, lease-based task
  ownership, per-message read state, and durable transcripts.

## Quick start

```bash
git clone https://github.com/Somainer/lyre.git
cd lyre
uv sync

# Set at least one provider key (DeepSeek is the cheapest path)
export DEEPSEEK_API_KEY=sk-...    # or ANTHROPIC_API_KEY, OPENAI_API_KEY

uv run lyre init                  # create DB + memory dir + seed agents
uv run lyre serve                 # scheduler + dispatcher + dashboard
```

In another terminal:

```bash
uv run lyre send leader "Hi leader, reply with pong and tell me what model you're on."
uv run lyre mailbox owner --unread-only      # see the reply
open http://127.0.0.1:8765                   # or browse the dashboard
```

See [docs/getting-started.md](./docs/getting-started.md) for the
five-minute walkthrough.

## Documentation

| Doc | Read this when… |
|---|---|
| [docs/getting-started.md](./docs/getting-started.md) | First-run walkthrough |
| [docs/concepts.md](./docs/concepts.md) | You want the mental model: agents vs personas, wakeups, mailbox, the five laws |
| [docs/configuration.md](./docs/configuration.md) | You need to set env vars, swap models, customize paths |
| [docs/writing-personas.md](./docs/writing-personas.md) | You're adding a new agent type, or writing skills |
| [docs/cli-reference.md](./docs/cli-reference.md) | You need a command reference + debug recipes |

Architecture design docs (Chinese) live under [docs/design/](./docs/design/):
`FOUNDATION.md`, `AGENT_CONTRACT.md`, `TRANSACTION_BOUNDARIES.md`,
`PERSISTENCE_SCHEMA.md`, `AGENT_RUNTIME.md`, `PERSONAS.md`, `DASHBOARD.md`.
They document the *why* behind the design decisions and serve as the
reference for contributors working on internals.

There's also a Chinese version of this README at [README.zh.md](./README.zh.md).

## Status

Lyre is under active development by a single owner. The five core
architectural laws (see [docs/concepts.md](./docs/concepts.md#the-five-laws))
are settled and won't move. APIs above that layer — persona format, available
tools, dashboard UI — are still evolving. Expect breaking changes between
commits until a first tagged release.

You're welcome to use, fork, learn from, and contribute back. See
[CONTRIBUTING.md](./CONTRIBUTING.md) for the development loop.

## License

[MIT](./LICENSE).
