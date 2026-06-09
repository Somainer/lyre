# Configuration

What Lyre reads from env vars, config files, and the filesystem.

## Environment variables

All env vars are optional unless noted. Set them in your shell or a
`.env` file at the repo root (Lyre auto-loads `.env` on startup).

### Auth (set at least one)

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Native Anthropic models (claude-opus / sonnet / haiku) |
| `DEEPSEEK_API_KEY` | DeepSeek models — works for both DeepSeek's Anthropic-compat endpoint AND their OpenAI-compat endpoint |
| `OPENAI_API_KEY` | OpenAI proper (gpt-5 / o-series); also accepted by OpenRouter etc. |

### Routing

| Variable | Purpose | Default |
|---|---|---|
| `LYRE_MODEL_OVERRIDE` | Force every wakeup to this specific `model_id`, ignoring persona routing. Useful for testing one provider. | unset (per-persona routing) |
| `LYRE_DEFAULT_MODEL` | Fallback if a persona doesn't specify a `model_preference`. | `claude-sonnet-4-6` |
| `LYRE_COMPACT_THRESHOLD` | Fraction of context window above which auto-compaction fires. Must be `0 < x < 1`. | `0.7` |
| `LYRE_MAX_TOKENS` | Per-turn output cap on a single assistant message (NOT a lifetime budget). Sized to the biggest single tool-call argument an agent writes — worker-maintainer producing code via `python_exec` is the hot path. Lower it only to box in a runaway worker. Min clamp 256. | `32768` |
| `LYRE_MAX_TURNS` | Default per-wakeup **turn** budget — the number of model↔tool-loop iterations a wakeup may run before it's honestly truncated to `needs_continuation` (distinct from `LYRE_MAX_TOKENS`, which caps one message). An orchestrator can raise it per-task via `dispatch_task(max_turns=…)` for work it expects to need many steps (e.g. a deep-research leg). Min clamp 1. | `24` |

### Scheduler

Env wins over the matching `[scheduler]` key in `config.toml`.

| Variable | Purpose | Default |
| --- | --- | --- |
| `LYRE_MAX_CONCURRENT_TASKS` | Max tasks the scheduler runs in parallel (subprocess mode only; inline mode is serial). `0`/negative clamps to 1. | `4` |
| `LYRE_IDLE_RECLAIM_AGE` | Seconds of idle (since last wakeup) after which `list_agents` marks a **spawned, non-ephemeral** agent `stale` — a hint the Dispatcher may `archive_agent` it. Pull-only: the runtime never auto-archives on this; bootstrap singletons and ephemeral agents (the reaper's job) are never flagged. `0` disables the hint entirely — fitting Lyre's "agents persist across restarts" default. | `0` (off) |
| `LYRE_FANIN_MAX_AGE` | Seconds — a **global** fan-in barrier TTL. When `> 0`, Phase 0.5 force-`expired`s any `open` fan_in_group older than this, regardless of the group's own (coordinator-set, up to 24h) `deadline`. A backstop / operator ceiling above the per-group deadline, which remains the always-on liveness. `0` disables it. | `0` (off) |
| `LYRE_NOTES_MAX_ENTRIES` | Max entries kept in an agent's `## Auto-summary log` before rotation. When `> 0`, each wakeup-end summary append that pushes the section past this count rotates the oldest entries down into the cold-archive tier (`object_store/notes_archive/agent-<id>.md`), keeping the hot notes file bounded so an agent reading its own notes can't blow the context window. The hand-written region above the log header is never touched. `0` disables rotation (notes grow forever). | `0` (off) |

### Coding backends (external coding agents)

Owner-declared credential bundles that let `shell_exec(credentials="<name>")`
inject one external coding-agent key into a single subprocess — so an agent can
drive `codex` / `claude` / `aider` / … headless. The **secret stays in
`~/.lyre/.env`** (same convention as model keys); config.toml holds only the
env-var *name*. Empty by default — no backend is reachable until you declare one.
See [docs/design/CAPABILITY_DISCOVERY.md](./design/CAPABILITY_DISCOVERY.md).

```toml
[coding_backends.codex]
auth_env = "OPENAI_API_KEY"            # env var holding the key
allowed_personas = ["worker-maintainer"]  # optional; omit = any persona with shell_exec

[coding_backends.claude]
auth_env = "ANTHROPIC_API_KEY"
```

> Known risk (accepted, single-owner model): the injected key is readable by the
> worker subprocess. True isolation is deferred to sandbox hardening.

### Storage paths

| Variable | What it controls | Default |
|---|---|---|
| `LYRE_DB_PATH` | SQLite file with tasks/wakeups/mailbox/etc. | `~/.lyre/lyre.db` |
| `LYRE_OBJECT_STORE` | Append-only artifact store (transcripts) | `~/.lyre/object_store/` |
| `LYRE_MEMORY_PATH` | Markdown filesystem the agents read/write | `~/.lyre/memory/` |

You can point all of these at a temp directory for isolated experiments:

```bash
export LYRE_HOME=/tmp/lyre-test           # one knob sets db, memory, object_store
uv run lyre onboard
uv run lyre serve
```

## Model registry

The shipped registry lives at `src/lyre/data/model_registry.yaml` (a
packaged resource — not for hand-editing). To add or override entries,
write `[[models]]` blocks in `~/.lyre/config.toml`. Same-id user entries
REPLACE the shipped entry; new ids append.

```yaml
models:
  - id: anthropic.claude-sonnet-4-6
    provider: anthropic              # which adapter module to use
    endpoint:
      base_url: null                 # null = SDK default
      auth_env: ANTHROPIC_API_KEY
    capabilities: [tool_use, streaming, long_context_1M]
    tier: workhorse                  # flagship | workhorse | cheap
    cost_per_mtok: { input: 3.00, output: 15.00 }
    context_window: 1000000
    status: enabled                  # enabled | disabled

  - id: deepseek-oai.deepseek-reasoner
    provider: openai                 # OpenAI /v1/chat/completions shape
    endpoint:
      base_url: https://api.deepseek.com/v1
      auth_env: DEEPSEEK_API_KEY
    capabilities: [tool_use, streaming, reasoning]
    tier: workhorse
    cost_per_mtok: { input: 0.55, output: 2.19 }
    context_window: 128000
    status: disabled                 # set to enabled if you have the key
```

### Provider field

| `provider` value | Adapter used | Endpoints it covers |
|---|---|---|
| `anthropic` | `src/lyre/adapter/anthropic.py` | Anthropic proper, DeepSeek's Anthropic-compat endpoint, Bedrock (with `base_url`) |
| `openai` | `src/lyre/adapter/openai.py` | OpenAI proper, DeepSeek's OpenAI-compat endpoint, OpenRouter, Together, vLLM, anything that speaks `/v1/chat/completions` |

### ID convention

`id` follows `<namespace>.<provider-model-name>`. The part **after** the
first dot is what gets sent to the API:

- `anthropic.claude-opus-4-7` → API receives `claude-opus-4-7`
- `deepseek-oai.deepseek-reasoner` → API receives `deepseek-reasoner`

This lets you have multiple entries pointing at the same underlying
model with different routing characteristics (e.g., `deepseek.deepseek-v4-pro`
via Anthropic-compat AND `deepseek-oai.deepseek-chat` via OpenAI-compat).

### Capabilities

Free-form strings personas can match against in their `model_preference.requires`.
Common ones used today: `tool_use`, `streaming`, `reasoning`,
`long_context_1M`. There's no enforced vocabulary — add what you need.

### Health and fallback

When a model fails or rate-limits, the `HealthTracker` (in
`src/lyre/runtime/health_tracker.py`) opens a circuit breaker on that
model. The router falls back to the next acceptable candidate from the
persona's preference list.

`lyre model list` shows current auth + health state for every entry.

## Persona files

After `lyre onboard` runs, personas live at `~/.lyre/personas/<name>/identity.md`
— that directory is the single source of truth. Lyre ships starter content
under `src/lyre/personas/` and copies it into your home dir on first
bootstrap; subsequent boots only fill in personas you've deleted (so your
edits / renames stick).

Each persona is a markdown file with YAML frontmatter:

```yaml
---
name: worker-maintainer
role_description: "Lyre 团队的 worker——在 per-task tmpdir 改代码、跑测试、提 PR"
allowed_lyre_tools:
  - python_exec
  - shell_exec
  - mailbox_send
  - mailbox_read
  - mailbox_get_message
  - mark_read
  - report_progress
  - report_side_effect
  - query_task_status
  - read_memory
  - list_agents
model_preference:
  tier: workhorse
  requires: [tool_use, streaming]
  prefer: [anthropic.claude-sonnet-4-6]
---
<persona system prompt body here, free-form markdown>
```

Field details:

| Field | What it does |
|---|---|
| `name` | Persona name. Must match filename stem. |
| `role_description` | One-line summary; shown to other agents in `list_personas()`. |
| `allowed_lyre_tools` | Subset of the runtime's tools this persona can call. Enforced at dispatch time — calling a non-allowlisted tool returns an error to the model. |
| `kind` | One of `singleton` / `seeded` / `spawn_only` — see [docs/design/PERSONAS.md](./design/PERSONAS.md). Controls bootstrap-agent seeding behaviour. |
| `model_preference.tier` | `flagship` / `workhorse` / `cheap`. Router picks among entries with this tier. |
| `model_preference.requires` | List of capability tags the chosen model must have. |
| `model_preference.prefer` | Explicit `model_id`s to try first within the matching tier. |

The body below the frontmatter is the persona's system prompt. It's
appended after the runtime-generated **identity preamble** (which
explains wakeups, mailbox protocol, ack-and-stop anti-pattern, etc.).

Personas are read directly from `~/.lyre/personas/<name>/identity.md`
on every persona lookup — there's no DB sync, the filesystem is the SSOT.
Edit identity.md and the change shows up on the next read; no restart
needed for content changes (config.toml overrides are loaded at process
start and DO require a restart).

**Two layouts supported** in `~/.lyre/personas/` (directory wins if both
exist for the same name):

  * `~/.lyre/personas/<name>/identity.md`  — preferred. Allows companion
    files in the same directory (e.g. `APPEND.md` to inject extra
    instructions at the bottom of the system prompt).
  * `~/.lyre/personas/<name>.md`           — legacy / minimal-fuss.

**Per-persona toml overrides** for single fields (model preference, allowed
tools): write `[personas.<name>]` in `~/.lyre/config.toml`:

```toml
[personas.leader]
model_preference = { prefer = ["anthropic.claude-opus-4-7"] }
allowed_lyre_tools = ["mailbox_send", "mailbox_read", "dispatch_task"]
```

For everything else (system_prompt, role_description, etc.) just edit
`identity.md` directly.

See [writing-personas.md](./writing-personas.md) for how to design new
personas, including the skills system.

## Memory directory layout

Created by `lyre onboard`. The agents have constrained read access via
`read_memory()` and arbitrary read/write via `shell_exec` / `python_exec`.

```
~/.lyre/
├── user.md                            # owner identity & preferences (user-write-only)
├── config.toml                        # owner identity, model overrides, paths
├── .env                               # API keys (chmod 600)
├── lyre.db                            # SQLite — runtime state only
├── personas/<name>/identity.md        # SSOT for personas (copied from shipped on onboard)
├── memory/                            # agent-write-only by convention
│   └── facts/
│       ├── agent-owner-notes.md       # owner's notebook (mostly for you)
│       ├── agent-leader-notes.md      # leader's cross-wakeup notebook
│       ├── agent-<worker-id>-notes.md # auto-created when each new agent is born
│       └── <other facts>.md           # ad-hoc facts agents drop here
└── skills/
    ├── approved/                      # active skills (loaded into prompts)
    │   ├── <skill-name>/SKILL.md      # PI-aligned: directory-per-skill
    │   └── ...
    └── proposed/                      # awaiting reviewer-skill approval
        └── ...
```

Authorship rules:

- `user.md` — **user writes only**, agents read it (injected into every
  system prompt). Edit freely; agents never overwrite it.
- `memory/` — **agents write only** (by convention; not enforced). You
  read but don't edit, so the agent doesn't get its notebook scrambled.

Important defaults:

- **Per-agent notes file.** When `create_agent` runs (or
  `seed_default_agents` on `lyre onboard`), a notes file is pre-created
  at `memory/facts/agent-<id>-notes.md`. This is the **Codex-style
  "pre-create the path, agent self-discovers"** pattern. The agent's
  identity preamble tells it explicitly that this file exists; the agent
  then naturally uses `read_memory()` / `shell_exec("cat >> ...")` to
  read and append.

- **Skills directory.** Markdown files describing reusable procedures.
  Each skill is a directory with a `SKILL.md` (frontmatter + body), per
  the PI Agent Skills standard. Skills appear in the system prompt as a
  collapsed XML menu (name + description only); the agent calls
  `read_memory()` to load full body on demand. See
  [writing-personas.md](./writing-personas.md#skills) for more.

## Where to put what

| Type of state | Lives in | Lifetime |
|---|---|---|
| In-flight task progress | `tasks.checkpoint` (JSON in DB) | Until task terminates |
| Mail in-flight | `mailboxes` + `mailbox_messages` (DB) | Forever (audit) |
| Wakeup transcripts | `~/.lyre/object_store/wakeups/<id>/transcript.jsonl` | Forever (audit) |
| Cross-wakeup agent notes | `~/.lyre/memory/facts/agent-<id>-notes.md` | Forever (owner can prune) |
| Owner identity / preferences | `~/.lyre/user.md` | Forever (user-edited) |
| Cross-task facts | `~/.lyre/memory/facts/*.md` | Forever (curated) |
| Reusable skills | `~/.lyre/memory/skills/approved/<name>/SKILL.md` | Forever (curated) |

The transactional boundary: at the end of every wakeup, the DB writes
(task status, outbox enqueue, etc.) commit atomically. The filesystem
writes (memory, transcripts) are append-only and individually
fsync'd — they're crash-safe but not transactionally consistent with
the DB. This is intentional; see the design doc `TRANSACTION_BOUNDARIES.md`
for why.
