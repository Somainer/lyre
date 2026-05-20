# Contributing to Lyre

Thanks for your interest. A few notes before you start.

## Project status

Lyre is currently a single-owner project under active development. I'll
review issues and PRs as time allows, but I can't promise quick turnarounds.
The five core architectural laws (see
[docs/concepts.md](./docs/concepts.md#the-five-laws)) are settled — PRs
that violate them won't land. Everything above that layer is open for
discussion.

## Development setup

```bash
git clone https://github.com/Somainer/lyre.git
cd lyre
uv sync --extra dev          # installs pytest, ruff, mypy, etc.
```

Verify everything works:

```bash
uv run pytest -q             # full test suite (~15 sec, 350+ tests)
uv run ruff check            # lint
```

Both must pass before a PR is merged. CI runs them on every push.

## How the runtime behaves locally

`uv run lyre onboard` runs an interactive wizard that writes
`~/.lyre/config.toml`, `~/.lyre/.env`, `~/.lyre/user.md`, and bootstraps
the DB. For isolated experiments, point Lyre at a tmpdir via `LYRE_HOME`:

```bash
export LYRE_HOME=/tmp/lyre-dev
uv run lyre onboard          # writes /tmp/lyre-dev/{config.toml, .env, ...}
uv run lyre serve
```

This lets you run a personal Lyre and a dev Lyre side by side without
mixing state.

## Where things live

| Path | Purpose |
|---|---|
| `src/lyre/adapter/` | LLM provider integrations (anthropic, openai) |
| `src/lyre/runtime/` | Agent loop, tools, scheduling helpers, memory, skills |
| `src/lyre/scheduler/` | Top-level scheduler / outbox dispatcher |
| `src/lyre/persistence/` | DAOs, schema, migrations |
| `src/lyre/dashboard/` | FastAPI + HTMX web UI |
| `src/lyre/personas/` | Persona markdown files (one per role) |
| `migrations/` | SQLite migrations, numbered `0NNN_*.sql` |
| `model_registry.yaml` | Provider/model entries |
| `tests/` | Unit + integration tests, mirrors `src/lyre/` |
| `docs/` | User-facing English documentation |
| `docs/design/` | Internal architecture design docs (Chinese): FOUNDATION, AGENT_CONTRACT, TRANSACTION_BOUNDARIES, PERSISTENCE_SCHEMA, AGENT_RUNTIME, PERSONAS, DASHBOARD |

## Style

- **Python**: ruff config in `pyproject.toml`. Run `uv run ruff check --fix`
  before pushing.
- **Comments**: write *why*, not *what*. Code says what. Comments earn their
  place by explaining hidden constraints, decisions, or surprises.
- **Tests**: every behavior change ships with a test. Aim for a test name
  that reads like a sentence (`test_loop_continues_after_tool_use_with_end_turn_stop_reason`).
  Tests are documentation of intended behavior; treat docstrings inside
  them seriously.
- **Migrations**: schema changes are append-only files under `migrations/`,
  numbered with the next ordinal. Don't edit a landed migration.

## Architecture invariants to preserve

If your PR is touching any of these, expect extra review:

- **Mailbox is the only inter-agent communication primitive.** No direct
  agent-to-agent calls. New tools should not bypass this.
- **Wakeups are stateless across boundaries.** The messages list is
  discarded when a wakeup ends. Cross-wakeup state goes in the mailbox,
  the memory filesystem, or `task.checkpoint`.
- **Kill-test as truth.** Adding state? Make sure it survives `SIGKILL`
  between any two writes.
- **No `end_turn` tool.** The agent loop ends when the model emits a
  response with no `tool_use` blocks. Don't reintroduce a callable
  "end this wakeup" tool.
- **Provider neutrality.** Adapter code is the only place that knows
  about a specific provider. The `LLMAdapter` interface is the seam.

## Filing issues

A good issue includes:

- What you ran (commands, env vars set)
- What happened (full output / error)
- What you expected
- Output of `uv run lyre wakeups list --since 30m` if it's an agent
  behavior issue
- The relevant wakeup transcript snippet if available
  (`uv run lyre audit --latest --persona X --json | jq ...`)

## PR checklist

- [ ] `uv run pytest -q` passes
- [ ] `uv run ruff check` passes
- [ ] New behavior is covered by a test
- [ ] Docstrings on new public functions explain *why*
- [ ] If you touched the schema, you added a migration
- [ ] If you touched the agent loop or compact, you read the relevant
      internal docs first

## A note on the internal docs

The `.md` files at the repo root are written in Chinese because that's
the language the project started in. They're the source of truth for
deep architecture decisions. If your PR involves them, you don't have
to translate — adding a one-line English summary alongside is enough.

## Questions

Open a GitHub Discussion or issue. There's no Slack / Discord — keep
things in writing, on the repo.
