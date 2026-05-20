# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What Lyre is

A long-running personal multi-agent runtime. Agents persist across restarts,
communicate only via durable SQLite-backed mailboxes, and pursue work across many
**wakeups** (discrete LLM tool-loop sessions). Provider-neutral across Anthropic,
OpenAI, DeepSeek, OpenRouter, vLLM — switching providers is a YAML edit.

Python 3.11+ (CI runs 3.12). Managed with `uv`.

## Commands

```bash
uv sync --extra dev              # install with pytest/ruff/mypy

uv run pytest -q                 # full suite (~15s, 350+ tests, fully offline)
uv run pytest tests/test_agent_loop.py::test_loop_continues_after_tool_use_with_end_turn_stop_reason
uv run pytest -k "compact"       # filter by name
uv run pytest --cov=lyre         # coverage

uv run ruff check                # lint (must pass before PR)
uv run ruff check --fix          # autofix
uv run mypy src                  # strict typing

uv run lyre onboard              # interactive setup -> ~/.lyre/{config.toml,.env,user.md,personas/,memory/}
uv run lyre serve                # scheduler + outbox dispatcher + dashboard (main runtime)
uv run lyre dashboard            # dashboard only (inspect state without running scheduler)
```

Both `ruff check` and `pytest -q` must pass — CI runs them on every push.

### Isolated dev runtime

Don't pollute your real `~/.lyre/`. Point `LYRE_HOME` at a tmpdir:

```bash
export LYRE_HOME=/tmp/lyre-dev
uv run lyre onboard && uv run lyre serve
```

### Common debug entry points

```bash
uv run lyre wakeups list --since 30m [--persona X] [--has-compaction] [--json]
uv run lyre audit --latest --persona <agent> [--json]   # transcript
uv run lyre tail --persona <agent>                       # live stream
uv run lyre tasks list --status in_progress
uv run lyre mailbox <agent_id> --unread-only
sqlite3 ~/.lyre/lyre.db ".tables"                        # full schema in PERSISTENCE_SCHEMA.md
```

`docs/cli-reference.md` has the full command list and debug recipes.

## Architecture: the five laws

These are **settled**. PRs that violate them won't land.

1. **Provider neutrality** — no code outside `src/lyre/adapter/` knows the
   provider. Adding a provider = one adapter module + one registry entry in
   `src/lyre/data/model_registry.yaml`. The `LLMAdapter` interface
   (`src/lyre/adapter/llm_adapter.py`) is the seam.
2. **Lyre is the gateway** — every agent → outside-world action goes through
   a Lyre tool (mail, shell, file I/O, GitHub). No sidechannels.
3. **Kill-test as truth** — at any instant, `SIGKILL`-ing any process must
   leave the system recoverable. This drives the outbox pattern, lease-based
   task ownership, per-message read state, durable transcripts.
4. **Persistence in three tiers**:
   - *Local-hot*: `task.checkpoint` (JSON in DB) — dropped when task ends.
   - *Global*: `~/.lyre/` markdown files (`user.md`, `personas/`, `skills/`,
     `memory/facts/`, per-agent notes).
   - *Cold*: append-only transcripts in `~/.lyre/object_store/`, never read
     back into runtime.
5. **Mailbox is the only inter-agent communication primitive** — owner→agent,
   agent→agent, scheduled mail. Dashboard, CLI, and agents are three clients
   of the same mailbox layer. No direct agent-to-agent calls.

## Architecture: the agent/persona/wakeup/task model

- **Persona** (`src/lyre/personas/<name>.md`) = static role definition with
  YAML frontmatter (`allowed_lyre_tools`, `model_preference`, `needs_worktree`)
  and a markdown system-prompt body. After onboarding it lives at
  `~/.lyre/personas/<name>/identity.md` (the SSOT).
- **Agent** = a running *instance* of a persona. One persona can have many
  agents (`worker-maintainer-1`, `worker-maintainer-2`...) each with its own
  mailbox, notes file, task queue. `mailbox_send` always targets an
  `agent_id`, never a persona name.
- **Wakeup** = one execution of the agent loop for one (agent, task) pair.
  Holds a lease on the task, runs the streaming LLM tool-loop. **Wakeups are
  stateless across boundaries** — the in-memory messages list is discarded
  when the wakeup ends. Cross-wakeup state lives in: the mailbox, the
  per-agent notes file at `~/.lyre/memory/facts/agent-<id>-notes.md`, or
  `task.checkpoint`.
- **Task** = a goal an agent pursues across many wakeups. States: `pending`
  → `in_progress` → (`needs_input` while awaiting subagents) →
  `completed`/`failed`/`cancelled`. Tasks can have `parent_task_id`, enabling
  parent-resume when subagents terminate.
- **Scheduler** (`src/lyre/scheduler/scheduler.py`) — three phases per tick:
  Phase −1 delivers due `scheduled_mail`; Phase 0 auto-wakes agents with
  unread mail; Phase 1 claims pending tasks, resumes parents, runs wakeups.
  Single-threaded async; SQLite WAL serializes writes.

### How a wakeup ends

The agent loop terminates when the model produces a response with **no
`tool_use` blocks**. There is no `end_turn` tool — **do not reintroduce
one**. `mailbox_send` does not yield the wakeup; the agent must stop calling
tools to end. The "ack-and-stop anti-pattern" (`"I'll look into it"` →
stop) is explicitly warned against in the runtime-generated identity preamble.

### Auto-compaction

When a wakeup's input tokens cross `LYRE_COMPACT_THRESHOLD` (default 0.7) of
the model's context window, `runtime/compact.py` rewrites the messages list
mid-flight: preserves all `mailbox_get_message` results (as synthetic user
msgs) and `mailbox_send` calls (as synthetic assistant msgs) verbatim,
summarizes the rest via one same-model LLM call, keeps the last 3 turn-pairs
intact. Per-wakeup metrics (peak context %, compaction count) land on the
`wakeups` row.

### System-prompt assembly order

`runtime/context.py` composes prompts in this order (stable→volatile, for
prompt-cache efficiency):

```
[identity preamble — auto-generated, byte-identical across an agent's wakeups]
[persona.role_description + persona body]
[~/.lyre/personas/<name>/APPEND.md if present]
[~/.lyre/SYSTEM.md if present]
[AGENTS.md walk from cwd upward]
[memory index — ## Available global memory]
[skills XML — collapsed name+description; full body loaded on demand via read_memory]
```

## Code layout

| Path | Purpose |
|---|---|
| `src/lyre/main.py` | Click CLI entrypoint (`lyre = "lyre.main:cli"`) |
| `src/lyre/adapter/` | LLM providers — only place that knows about Anthropic/OpenAI specifics |
| `src/lyre/runtime/agent_loop.py` | The wakeup loop: streaming, tool dispatch, fallback, interrupts |
| `src/lyre/runtime/compact.py` | Mid-flight context compaction |
| `src/lyre/runtime/context.py` | System-prompt assembly |
| `src/lyre/runtime/tools/` | Built-in tools (`mailbox.py`, `shell.py`, `python.py`, `tasks.py`, `progress.py`, `introspect.py`) |
| `src/lyre/runtime/model_router.py` + `model_registry.py` + `health_tracker.py` | Persona → ranked model candidates, circuit-breaker fallback |
| `src/lyre/scheduler/scheduler.py` | Top-level scheduler/dispatcher loop |
| `src/lyre/outbox/dispatcher.py` | Async outbox→mailbox delivery (idempotent via `external_id`) |
| `src/lyre/persistence/` | DAOs (`sqlite_impl.py`), Pydantic models, schema bootstrap |
| `src/lyre/dashboard/` | FastAPI + HTMX + SSE web UI |
| `src/lyre/personas/` | Shipped persona markdowns (copied to `~/.lyre/` on onboard) |
| `src/lyre/data/model_registry.yaml` | Packaged provider/model entries |
| `migrations/` | SQLite migrations, numbered `0NNN_*.sql`, **append-only** |
| `tests/` | Mirrors `src/lyre/`; `conftest.py` + `fake_adapter.py` + `helpers.py` are shared fixtures |
| `docs/design/` | Chinese architecture docs — source of truth for *why* decisions |

## Conventions

- **Comments: write *why*, not *what*.** Code says what. Comments earn their
  place by explaining hidden constraints, decisions, or surprises.
- **Tests**: every behavior change ships with a test. Name tests like
  sentences (`test_loop_continues_after_tool_use_with_end_turn_stop_reason`).
  Test suite is designed to run **fully offline** — no provider keys needed,
  adapter integration uses `tests/fake_adapter.py`.
- **Migrations are append-only.** Schema changes go in a new
  `migrations/0NNN_*.sql` with the next ordinal. Never edit a landed
  migration.
- **Ruff config** in `pyproject.toml`: target py311, line-length 100,
  selects `E,F,I,B,UP,ASYNC,SIM`. Ignored: `E501` (formatter handles),
  `ASYNC240` (pathlib in async fns is fine for cwd/dir ops), `SIM102/SIM105`
  (clarity over chained `and` / `contextlib.suppress`).
- **Mypy**: strict mode.
- **`pyproject.toml`** is the package manifest — `src/lyre/` is a packaged
  module via hatchling.

## When touching these subsystems

- **Agent loop or compact** (`runtime/agent_loop.py`, `runtime/compact.py`)
  — read `docs/design/AGENT_RUNTIME.md` and `docs/design/AGENT_CONTRACT.md`
  first. The thinking-block-to-tool-use binding constraint (Anthropic
  extended-thinking API) is subtle.
- **Persistence schema** — read `docs/design/PERSISTENCE_SCHEMA.md` and
  `docs/design/TRANSACTION_BOUNDARIES.md`. The DB writes at end-of-wakeup
  commit atomically; filesystem writes (memory, transcripts) are append-only
  and individually fsync'd. This split is intentional.
- **Adapters** — `LLMAdapter` interface in `adapter/llm_adapter.py` is the
  only seam. New providers need: adapter module, entries in
  `src/lyre/data/model_registry.yaml`, capability tags, fallback test.
- **Personas** — shipped persona changes go in `src/lyre/personas/<name>.md`;
  user-edited copies live at `~/.lyre/personas/<name>/identity.md`. The
  onboard wizard copies the shipped version on first run and never
  overwrites afterwards.

## Configuration knobs

Set in shell or `.env` (auto-loaded). Full list in `docs/configuration.md`.

| Var | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` | Provider auth (at least one) |
| `LYRE_MODEL_OVERRIDE` | Force every wakeup to one `model_id` (testing) |
| `LYRE_DEFAULT_MODEL` | Fallback when persona has no `model_preference` (default `claude-sonnet-4-6`) |
| `LYRE_COMPACT_THRESHOLD` | Compaction trigger as fraction of context window (default `0.7`) |
| `LYRE_HOME` / `LYRE_DB_PATH` / `LYRE_OBJECT_STORE` / `LYRE_MEMORY_PATH` | Storage paths |

## Internal design docs

`docs/design/` (Chinese — source of truth for architecture decisions):
`FOUNDATION.md`, `AGENT_CONTRACT.md`, `TRANSACTION_BOUNDARIES.md`,
`PERSISTENCE_SCHEMA.md`, `AGENT_RUNTIME.md`, `PERSONAS.md`, `DASHBOARD.md`.
Don't translate when editing — a one-line English summary alongside is enough.
