# CLI Reference

Every `lyre` command, plus debug recipes for the most common questions
("why didn't my agent reply?", "what did the model say?", "is anything
stuck?").

## Commands by purpose

### Lifecycle

| Command | Purpose |
|---|---|
| `lyre init` | Create DB schema, memory dir skeleton, bootstrap agents (`owner`, `leader`). Idempotent — safe to re-run. |
| `lyre serve [--no-dashboard] [--dashboard-port N]` | Run scheduler + outbox dispatcher (+ dashboard). The main runtime entry point. |
| `lyre dashboard [--host H] [--port N]` | Run only the dashboard (no scheduler). For inspecting state without starting the runtime. |

### Sending and reading mail

| Command | Purpose |
|---|---|
| `lyre send <agent_id> "<body>" [--title T] [--urgency normal\|high\|blocker\|low] [--reply-to N]` | Send a mail. Always from `owner`. |
| `lyre mailbox <agent_id> [--unread-only] [--since N]` | List mail in an agent's inbox. |

### Agent management

| Command | Purpose |
|---|---|
| `lyre agent list [--include-archived]` | List agent instances (NOT personas). |
| `lyre agent create <persona> [--name N] [--model M] [--description D]` | Create a new agent instance of a persona. |
| `lyre agent archive <agent_id>` | Soft-delete an agent (bootstrap agents `owner` / `leader` can't be archived). |

### Inspection (the debug entry points)

| Command | Purpose |
|---|---|
| `lyre wakeups list [--limit N] [--persona P] [--status S] [--since 1h] [--has-compaction] [--json]` | List recent wakeups with status, tokens, context-usage %, compaction count. The first thing you run when something feels off. |
| `lyre tasks list [--limit N] [--persona P] [--agent A] [--status S] [--since 1h] [--json]` | List recent tasks with status and goal preview. |
| `lyre status <task_id>` | Single-task detail: checkpoint, all wakeups for it, latest transcript. |
| `lyre audit <wakeup_id_prefix> [--system/--no-system] [--full-result] [--json]` | Pretty-print a wakeup's transcript. `--latest [--persona P]` for the most recent. `--json` for raw JSONL. |
| `lyre tail [--persona P] [--active-only] [--json]` | Live `tail -f`–style stream of an active wakeup's transcript. |

### Future mail

| Command | Purpose |
|---|---|
| `lyre mail list-scheduled [--recipient R] [--sender S] [--status S]` | List pending scheduled mail. |
| `lyre mail cancel <id> [--reason R]` | Cancel a pending scheduled mail (stops recurring schedules too). |

### Models

| Command | Purpose |
|---|---|
| `lyre model list` | Show the model registry + per-entry auth health. |

### Other (mostly internal)

| Command | Purpose |
|---|---|
| `lyre dispatch <persona> <goal> <acceptance>` | Directly enqueue a task. You usually don't need this — leader does it via `dispatch_task`. |
| `lyre run-task <task_id>` | (Hidden) Subprocess mode for one specific task. Used internally when `lyre serve --subprocess` is enabled. |

## Output formats

Most inspection commands take `--json`. The default is a human-readable
table; `--json` emits JSON Lines (one record per line) for piping to
`jq`.

```bash
# Latest 10 leader wakeups, just the IDs
lyre wakeups list --persona leader --limit 10 --json | jq -r '.id'

# Find all wakeups that compacted (had to summarize mid-flight)
lyre wakeups list --has-compaction --since 24h --json | \
  jq -r '"\(.id) compacted ×\(.compaction_count) (peak \(.context_peak_pct)%)"'

# All tool calls from the most recent leader wakeup
lyre audit --latest --persona leader --json | \
  jq -r 'select(.type=="tool_use") | "\(.name)(\(.input|tostring|.[:80]))"'

# Extract just the model's reasoning from a wakeup
lyre audit <wakeup-id> --json | \
  jq -r 'select(.type=="thinking_delta") | .text' | head -c 4000
```

## Debug recipes

### "Why didn't my agent reply?"

```bash
# 1. Did the wakeup happen at all?
lyre wakeups list --persona <agent> --since 10m

# If status=silent_close, that's the runtime's "ran but produced no
# reply" signal — there should be a fallback mail in your inbox.
lyre mailbox owner --since 10m

# 2. If completed but no reply: see what tools the model actually called
lyre audit --latest --persona <agent> --json | \
  jq -r 'select(.type=="tool_use") | .name' | sort | uniq -c

# A healthy reply chain has exactly one mailbox_send. Zero = agent did
# work but never spoke. Many = something went wrong with mail.

# 3. See the model's thinking — often diagnoses the issue
lyre audit --latest --persona <agent>
```

### "Is anything stuck?"

```bash
# In-progress tasks (running RIGHT NOW)
lyre tasks list --status in_progress

# Tasks waiting for subagents
lyre tasks list --status needs_input --since 24h

# Active wakeups (still streaming)
sqlite3 ~/.lyre/lyre.db "SELECT id, persona_name, started_at FROM wakeups WHERE ended_at IS NULL;"
```

### "Did the model hit context limit?"

```bash
# Wakeups close to or over the threshold
lyre wakeups list --since 24h --json | \
  jq -r 'select(.context_peak_pct) | select(.context_peak_pct > 50) | "\(.id) \(.persona_name) peak=\(.context_peak_pct)% compacted=\(.compaction_count)"'

# All compactions in the last day
lyre wakeups list --has-compaction --since 24h
```

### "Watch a long-running wakeup live"

```bash
# Pretty stream
lyre tail --persona <agent>

# Raw JSONL for filtering
lyre tail --persona <agent> --json | jq 'select(.type=="tool_use")'
```

### "What did agent X commit to that hasn't happened?"

```bash
# Look at the agent's notes file — agents should write commitments here
cat ~/.lyre/memory/facts/agent-<id>-notes.md

# Or look at recent outbound mail
sqlite3 ~/.lyre/lyre.db "
  SELECT id, datetime(delivered_at), recipient, title, body
  FROM mailbox_messages
  WHERE sender = '<agent-id>'
  ORDER BY id DESC LIMIT 10;
"
```

### "Reset everything"

For a clean experiment without losing the global setup:

```bash
# Soft reset — drop the DB, keep memory + transcripts
rm ~/.lyre/lyre.db ~/.lyre/lyre.db-{wal,shm}
uv run lyre init

# Full reset — drop everything
rm -rf ~/.lyre
uv run lyre init
```

Or point everything at a tmpdir for the session:

```bash
LYRE_DB_PATH=/tmp/lyre-exp/db.sqlite \
LYRE_OBJECT_STORE=/tmp/lyre-exp/obj \
LYRE_MEMORY_PATH=/tmp/lyre-exp/mem \
  uv run lyre init && \
LYRE_DB_PATH=/tmp/lyre-exp/db.sqlite \
LYRE_OBJECT_STORE=/tmp/lyre-exp/obj \
LYRE_MEMORY_PATH=/tmp/lyre-exp/mem \
  uv run lyre serve
```

### "Inspect the schema directly"

When the CLI doesn't have a query you need, drop to SQL:

```bash
sqlite3 ~/.lyre/lyre.db ".tables"
sqlite3 ~/.lyre/lyre.db ".schema wakeups"

# Recent inter-agent traffic
sqlite3 ~/.lyre/lyre.db "
  SELECT id, datetime(delivered_at), sender, recipient, urgency,
         substr(title, 1, 50) AS title
  FROM mailbox_messages
  ORDER BY id DESC LIMIT 20;
"

# Outbox backlog (should be near 0)
sqlite3 ~/.lyre/lyre.db "
  SELECT COUNT(*) FROM outbox WHERE dispatched_at IS NULL;
"
```

The full schema is documented in `PERSISTENCE_SCHEMA.md` at the repo
root. Schema version is tracked in the `schema_migrations` table.

## Environment cheatsheet

```bash
# Switch all wakeups to a specific cheap model for testing
LYRE_MODEL_OVERRIDE=deepseek.deepseek-v4-flash lyre serve

# Tighter compact threshold (compact more aggressively)
LYRE_COMPACT_THRESHOLD=0.5 lyre serve

# Run against an isolated DB
LYRE_DB_PATH=/tmp/lyre-isolated.db lyre serve
```

See [configuration.md](./configuration.md#environment-variables) for the
full list.
