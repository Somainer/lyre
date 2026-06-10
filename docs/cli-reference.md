# CLI Reference

Every `lyre` command, plus debug recipes for the most common questions
("why didn't my agent reply?", "what did the model say?", "is anything
stuck?").

## Commands by purpose

### Lifecycle

| Command | Purpose |
|---|---|
| `lyre onboard` | Interactive wizard: writes `~/.lyre/config.toml` + `.env` + `user.md`, copies shipped personas to `~/.lyre/personas/<name>/identity.md`, bootstraps DB + memory dirs + seeded agents (`owner`, `dispatcher`, `analyst-1`, `reviewer-1`). Safe to re-run. |
| `lyre serve [--poll-interval S] [--no-dashboard] [--dashboard-host H] [--dashboard-port N] [--no-subprocess]` | Run scheduler + outbox dispatcher (+ dashboard). The main runtime entry point. Each task runs in a fresh `lyre run-task` subprocess by default; `--no-subprocess` runs them inline (debugging). |
| `lyre dashboard [--host H] [--port N]` | Run only the dashboard (no scheduler). For inspecting state without starting the runtime. |
| `lyre maintenance [--retention-days N] [--no-vacuum]` | Prune terminal/delivered DB rows past the retention window, checkpoint the WAL, VACUUM. The scheduler also runs this automatically (without VACUUM) when `retention_days > 0`. |
| `lyre persona-refresh <name>` / `lyre persona-refresh --all [--no-backup]` | Re-copy a shipped persona over `~/.lyre/personas/<name>/identity.md` (onboard never overwrites it). Backs up the current identity.md first unless `--no-backup`. |

### Sending and reading mail

| Command | Purpose |
|---|---|
| `lyre send <agent_id> "<body>" [--title T] [--urgency blocker\|high\|normal\|low] [--from S] [--task-id ID] [--thread-id ID] [--no-spawn]` | Send a mail. Sender defaults to `owner` (`--from` overrides). Unknown `persona/name` recipients are auto-created unless `--no-spawn`. |
| `lyre send ... [--at <ISO8601>] [--in 2h] [--recur-every 1d] [--recur-cron "0 9 * * 1-5"] [--until <ISO8601>]` | Future / recurring mail: schedule the delivery instead of sending now (`--at` absolute, `--in` relative; `--recur-every` and `--recur-cron` are mutually exclusive). |
| `lyre mailbox [agent_id] [--unread-only] [--since N]` | List mail in an agent's inbox (defaults to `owner`). `--since` is a message-id watermark (only `id > N`), not a time. |

### Agent management

| Command | Purpose |
|---|---|
| `lyre agent list [--all]` | List agent instances (NOT personas). `--all` includes archived ones. |
| `lyre agent create <persona> [--name N] [--model M] [--description D]` | Create a new agent instance of a persona (`--name` sets the full agent id; omitted → auto `<persona>/<n>`). |
| `lyre agent archive <agent_id>` | Soft-archive **any** agent; in-flight tasks finish. (Only the agent-facing `archive_agent` tool refuses seeded agents.) If you archive a singleton like `dispatcher`, restarting `lyre serve` re-seeds it. |
| `lyre agent unarchive <agent_id>` | Bring an archived agent back to `idle`. Mail history is preserved; idempotent. |

### Inspection (the debug entry points)

| Command | Purpose |
|---|---|
| `lyre wakeups list [--limit N] [--persona P] [--status S] [--since 1h] [--has-compaction] [--summary-degraded] [--json]` | List recent wakeups with status, tokens, context-usage %, compaction count, and degraded-compaction count. The first thing you run when something feels off. `--summary-degraded` filters to wakeups where a compaction's work-summary LLM call failed (lossy compaction). |
| `lyre tasks list [--limit N] [--persona P] [--agent A] [--status S] [--since 1h] [--json]` | List recent tasks with status and goal preview. |
| `lyre tasks cancel <task_id> [--reason R]` | Cooperative cancel of a running / pending task. The wakeup observes it at its next turn boundary and stops with status `cancelled`. Cancels the TASK, not the agent. |
| `lyre status <task_id>` | Single-task detail: checkpoint, all wakeups for it, latest transcript. |
| `lyre audit <wakeup_id_prefix> [--system/--no-system] [--full-result] [--json]` | Pretty-print a wakeup's transcript. `--latest [--persona P]` for the most recent. `--json` for raw JSONL. |
| `lyre tail [--persona P] [--active-only/--include-completed] [--poll S] [--no-follow] [--json]` | Live `tail -f`–style stream of an active wakeup's transcript. Follows subsequent wakeups by default (`--no-follow` to stop at the first ended one). |

### Future mail

| Command | Purpose |
|---|---|
| `lyre mail list-scheduled [--recipient R] [--sender S] [--status pending\|completed\|cancelled\|bounced\|all] [--limit N]` | List scheduled mail entries. |
| `lyre mail cancel <id> [--reason R]` | Cancel a pending scheduled mail (stops recurring schedules too). |

### Models

| Command | Purpose |
|---|---|
| `lyre model list` | Show the model registry + per-entry auth health. |

### Other (mostly internal)

| Command | Purpose |
|---|---|
| `lyre dispatch <persona> <goal> [--acceptance A]` | Directly enqueue a task. You usually don't need this — the dispatcher does it via `dispatch_task`. |
| `lyre run-task <task_id>` | (Hidden) Run one task to completion in this process. Spawned per task by `lyre serve` (subprocess mode is the default). |

## Output formats

Most inspection commands take `--json`. The default is a human-readable
table; `--json` emits JSON Lines (one record per line) for piping to
`jq`.

```bash
# Latest 10 dispatcher wakeups, just the IDs
lyre wakeups list --persona dispatcher --limit 10 --json | jq -r '.id'

# Find all wakeups that compacted (had to summarize mid-flight)
lyre wakeups list --has-compaction --since 24h --json | \
  jq -r '"\(.id) compacted ×\(.compaction_count) (peak \(.context_peak_pct)%)"'

# All tool calls from the most recent dispatcher wakeup
lyre audit --latest --persona dispatcher --json | \
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

# Pending backlog (queued, not yet claimed)
lyre tasks list --status pending --since 24h

# Active wakeups (still streaming)
sqlite3 ~/.lyre/lyre.db "SELECT id, persona_name, started_at FROM wakeups WHERE ended_at IS NULL;"

# Genuinely wedged? Cancel the task cooperatively (agent stays alive)
lyre tasks cancel <task_id> --reason "stuck, will re-dispatch"
```

(There is no "waiting for subagents" status to query: delegation is
mail-driven — a delegating agent's task completes when it stops calling
tools, and the reply mail wakes it later. `needs_input` exists in the
schema but nothing sets it today.)

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
uv run lyre onboard          # re-runs wizard; pick "keep existing" for config

# Full reset — drop everything
rm -rf ~/.lyre
uv run lyre onboard
```

Or point everything at a tmpdir for the session via `LYRE_HOME`:

```bash
LYRE_HOME=/tmp/lyre-exp uv run lyre onboard
LYRE_HOME=/tmp/lyre-exp uv run lyre serve
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

The full schema is documented in `docs/design/PERSISTENCE_SCHEMA.md`.
Schema version is tracked in the `schema_migrations` table.

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
