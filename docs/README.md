# Lyre Documentation

Lyre is a long-running multi-agent runtime for a single owner. Agents persist
across restarts, communicate only via durable mailboxes, and pursue work that
spans many model wakeups — not just one chat session.

This directory holds the user-facing English documentation. Internal design
docs (`FOUNDATION.md`, `AGENT_CONTRACT.md`, `PERSISTENCE_SCHEMA.md`, etc., in
Chinese) live at the repository root.

## Where to start

| If you want to… | Read |
|---|---|
| Run Lyre in 5 minutes | [getting-started.md](./getting-started.md) |
| Understand the mental model | [concepts.md](./concepts.md) |
| Configure models, paths, env vars | [configuration.md](./configuration.md) |
| Add a new agent type | [writing-personas.md](./writing-personas.md) |
| Debug a stuck wakeup | [cli-reference.md](./cli-reference.md) |

## What Lyre is

- **Long-running.** A task isn't one prompt; it's a goal an agent pursues
  across many wakeups, days, restarts. The runtime keeps task state, mailbox
  state, and crash recovery on disk so progress survives anything short of
  losing the database.

- **Mailbox-native.** Agents have no other way to communicate. They can't
  call each other directly; they send mail. Owner→agent and agent→agent
  traffic both flow through the same persistent mailbox. The dashboard and
  CLI are just two views of that mailbox.

- **Multi-agent, single-owner.** Lyre runs a small team — leader + workers
  + reviewers, however you set it up — but everything serves *you*, not an
  organization. There are no permissions to negotiate, no audit committee,
  no compliance surface.

- **Provider-neutral.** Anthropic, DeepSeek (via either their Anthropic-compat
  or OpenAI-compat endpoints), OpenAI, OpenRouter, vLLM-served models — all
  routed through the same `LLMAdapter` interface. Switching providers is a
  YAML edit.

## What Lyre is not

- **Not an agent framework** like LangGraph / AutoGen / CrewAI. It's not for
  programmable graphs of tools; it's a *runtime* that hosts long-lived
  agents the same way a process supervisor hosts daemons.
- **Not for organizations.** No multi-tenant isolation, no RBAC.
- **Not a chatbot.** Lyre agents pursue work autonomously between your
  messages. You're not the only one driving them — the scheduler, mail
  delivery, and other agents also wake them.

## Status

Lyre is under active development. The five core laws (see
[concepts.md](./concepts.md)) are settled and won't move. APIs above that
layer — persona format, available tools, dashboard UI — are still evolving.

## License

MIT. See [`LICENSE`](../LICENSE) in the repo root.
