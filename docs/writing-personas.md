# Writing Personas

How to add a new agent type to your Lyre team.

A **persona** is a single markdown file at `src/lyre/personas/<name>.md`
that defines a role. Once a persona exists in the database, anyone (you,
or another agent with `create_agent` permission) can spawn agent
instances of that role.

This page covers:

1. [File format](#file-format)
2. [What lives in the system prompt](#what-lives-in-the-system-prompt)
3. [The identity preamble (auto-prepended)](#the-identity-preamble)
4. [Mental model the agent must have](#mental-model-the-agent-must-have)
5. [Allowed tools](#allowed-tools)
6. [Skills](#skills)
7. [Worked example](#worked-example)

## File format

```yaml
---
name: reviewer-pr
role_description: "Lyre 团队的 PR reviewer——评审 worker 开的 PR"
allowed_lyre_tools:
  - python_exec
  - shell_exec
  - mailbox_send
  - mailbox_read
  - mailbox_get_message
  - mark_read
  - report_side_effect
  - read_memory
  - list_agents
model_preference:
  tier: workhorse
  requires: [tool_use, streaming]
  prefer: [anthropic.claude-sonnet-4-6]
status: approved
---

You are Lyre's PR reviewer. When a worker opens a PR, you're dispatched
to review it...

[the rest of the prompt body, free-form markdown]
```

Field reference is in [configuration.md](./configuration.md#persona-files).
`status: approved` is the default; you generally won't set anything else.

## What lives in the system prompt

At wakeup time, the runtime assembles the system prompt in this order:

```
[identity preamble — auto-generated]
[persona.role_description]
[persona system prompt body]
[~/.lyre/personas/<name>/APPEND.md if present]    # owner-level overrides
[~/.lyre/SYSTEM.md if present]                    # deployment-wide notes
[AGENTS.md walk from cwd upward]                  # project-local context
[memory index]                                    # ## Available global memory
[skills XML]                                      # collapsed skill menus
```

You only write the persona body. Everything else is composed by the
runtime. The order minimizes cache invalidation: stable parts first
(identity preamble + persona body are byte-identical across wakeups
for the same agent), volatile parts at the tail (memory index changes
when files are added/edited).

## The identity preamble

Every persona's system prompt starts with a runtime-generated section
you don't have to write. It includes (verbatim, per agent):

- The agent's id and persona name; warning not to invent variants
- **HOW WAKEUPS END** — explanation that there's no `end_turn` tool;
  the wakeup ends when the model emits a response with no tool_use
- **HOW YOU COMMUNICATE** — plain text isn't delivered; mailbox_send is
- **MAIL PROTOCOL** — `mailbox_read` auto-marks; `box="sent"` for recall;
  always supply a clear title
- **ACK-AND-STOP IS A LIE** — the most common Lyre failure mode, named
  with examples ("I'll look into X" / "starting a background task")
- **KNOWING THE TEAM** — pointer to `list_agents()` for the current roster
- **STATELESS WAKEUPS** — explains durability: mailbox + memory files
  survive; in-memory reasoning doesn't
- **DELEGATING WORK** — three invariants: no phantom delegation,
  always report before idling, track delegated tasks in notes
- **PROGRESS VIA MAIL** — long-running work emits periodic progress mail

If you find yourself wanting to repeat any of this in your persona
body, stop — the preamble covers it. Save persona-body bytes for
role-specific instructions.

You can inspect the preamble for a given persona via:

```bash
uv run python -c "
from lyre.persistence.models import Persona
from lyre.runtime.context import _build_identity_preamble
print(_build_identity_preamble('your-agent-id', 'your-persona-name'))
"
```

## Mental model the agent must have

The bits you DO want to teach in the persona body:

### Role and scope

What does this agent do, and what does it NOT do?

```markdown
You are Lyre's PR reviewer. Your job:
- clone the PR's branch into your worktree
- run the project's tests
- read the diff and identify risks
- write review comments via `mark_pr_reviewed`
- on critical risk, escalate to leader via `mailbox_send urgency=high`

You DO NOT merge PRs. You DO NOT modify code. Your output is structured
review, not patches.
```

### Communication targets

Who does this agent typically talk to? Usually:

- **Owner** — for status / blockers
- **Leader** — for work coordination
- **Other workers** — rarely; usually via leader

Make this explicit in the prompt.

### Failure modes specific to the role

If the role has known pitfalls, name them:

```markdown
**Common mistake**: skipping the test run because the diff "looks fine".
Never approve a PR without running the test suite to completion in the
worktree. If tests are slow, that's fine — log progress, don't skip.
```

### Style

Optional but useful. Length / structure expectations for the agent's
mailbox output.

## Allowed tools

Every persona declares which Lyre tools it can call. The runtime
enforces this — a call to a non-allowlisted tool returns a `ToolError`
to the model.

### Required for every persona

These are universal — every persona must include them, or the identity
preamble lies to the agent:

- `mailbox_send` — the only way to communicate
- `mailbox_read` — see your inbox
- `mailbox_get_message` — fetch full mail body
- `mark_read` — dismiss FYI mail
- `read_memory` — read the notes file the runtime pre-creates
- `list_agents` — see the team (the preamble points here)

### Adding more

Add whatever role-specific tools the persona needs:

| Tool | Purpose |
|---|---|
| `shell_exec` | Run arbitrary shell commands (`git`, `gh`, `make`, etc.) |
| `python_exec` | Run a Python snippet (file ops, HTTP, parsing) |
| `dispatch_task` | Spawn a child task (dispatcher / analyst / reviewer; workers are leaves) |
| `query_task_status` | Inspect any task — used to poll child task progress |
| `create_agent` / `archive_agent` | Manage agent instances |
| `list_personas` / `list_tasks` / `list_models` | Introspection |
| `report_progress` | Crash-recovery checkpoint (NOT visible externally) |
| `report_side_effect` | Self-report external side effects (PR opened, branch pushed) |
| `list_scheduled_mail` / `cancel_scheduled_mail` | Future-mail mgmt |

Withholding tools is a way to scope responsibility. A `reviewer-pr`
shouldn't have `dispatch_task` (it shouldn't be spawning workers); a
`summary-agent` shouldn't have `shell_exec` (it should only touch
mailbox + memory files via `read_memory` and a few targeted writes).

### Persona-allowlist alignment test

The repo includes a test
(`tests/test_persona_allowlist_alignment.py`) that fails the build if
any persona is missing the universal six. If you add a new persona,
this test will tell you what you forgot.

## Skills

Lyre implements the [PI Agent Skills](https://github.com/earendil-works/pi)
standard. A skill is a directory in
`~/.lyre/memory/skills/approved/<name>/` containing a `SKILL.md` with
YAML frontmatter and a markdown body.

### Layout

```
~/.lyre/memory/skills/approved/
└── update-dependencies/
    └── SKILL.md
```

`SKILL.md` example:

```markdown
---
name: update-dependencies
description: Bump dependencies in pyproject.toml + uv lock + verify tests pass
scope: global               # global | persona:<name> | task:<id>
when_to_use: When asked to bump deps or address security advisories
---

1. Read current versions:
   `uv run python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies'])"`

2. Check upstream latest:
   `uv pip list --outdated`

3. Update pyproject.toml conservatively...

[detailed procedure]
```

### How agents see skills

The system prompt includes a **menu only** — the skill name and
description, in XML:

```xml
<skills>
  <skill name="update-dependencies">Bump dependencies in pyproject.toml + uv lock + verify tests pass</skill>
  ...
</skills>
```

The agent decides whether the skill is relevant for the current task.
If yes, it calls `read_memory(rel_path="skills/approved/update-dependencies/SKILL.md")`
to load the full body and follow the procedure.

This is **progressive disclosure**: only descriptions in the prompt
(cheap), bodies fetched on demand (only when needed).

### Approving skills

The bundled `reviewer-skill` persona reviews proposed skills in
`~/.lyre/memory/skills/proposed/` and moves them to `approved/` or
deletes them. You can also approve manually with `git mv`.

## Worked example

Adding a new `web-researcher` persona that searches the web and writes
notes:

1. **Create `src/lyre/personas/web-researcher.md`**:

   ```yaml
   ---
   name: web-researcher
   role_description: "Lyre researcher who pulls web content into structured notes"
   allowed_lyre_tools:
     - python_exec        # for httpx / BeautifulSoup
     - mailbox_send
     - mailbox_read
     - mailbox_get_message
     - mark_read
     - read_memory
     - list_agents
     - report_progress
   model_preference:
     tier: workhorse
     requires: [tool_use, streaming]
     prefer: [anthropic.claude-sonnet-4-6, deepseek.deepseek-v4-pro]
   ---

   You are Lyre's web researcher. When dispatched, you fetch URLs, parse
   them, and write a structured notes file into
   `~/.lyre/memory/facts/research-<topic>-<date>.md`.

   ## Workflow

   1. Read `mailbox_get_message` to get the full task brief from leader.
   2. Use `python_exec` with httpx + BeautifulSoup to fetch each URL.
      Respect robots.txt; skip behind-paywall content with a one-line note.
   3. Extract key facts, quotes, and links.
   4. Write the notes file with frontmatter:
      `---\nname: research-<topic>-<date>\ndescription: <one-line summary>\ntype: facts\nsources: [urls]\n---`
   5. `mailbox_send` back to whoever dispatched you with:
      - File path
      - One-paragraph summary
      - List of sources actually used (with URL + status)
      - Any "couldn't access" warnings

   ## Tools

   `python_exec` is your only fetching tool. You don't have `shell_exec`
   — no `curl`/`wget`. If you need a header / cookie that httpx can't
   handle, mailbox_send leader for guidance.

   ## Style

   Notes files are for FUTURE agents to consult. Write them in a way
   that's useful to someone who isn't you and doesn't have your reasoning
   context. Prefer bullet lists with sources over flowing prose.
   ```

2. **Re-seed the personas table.** Personas auto-upsert on every
   `lyre serve` start, so the simplest path is just restart serve. If
   you want a one-shot bootstrap from a clean state, use
   `lyre onboard` — it upserts personas as part of its bootstrap step
   (re-run is safe; existing config.toml stays unless you confirm
   overwrite).

   ```bash
   uv run lyre onboard
   ```

3. **Verify the persona is registered**:

   ```bash
   uv run lyre model list           # see what models it can route to
   ```

4. **Spawn an instance + give it work** — typically via leader:

   ```bash
   uv run lyre send leader "Please dispatch a web-researcher to look up
   the rate limits documented on the OpenAI, Anthropic, and DeepSeek
   pricing pages, and write me a notes file."
   ```

   Leader will `create_agent(persona="web-researcher")` → call
   `dispatch_task(agent="web-researcher-1", goal="...", acceptance="...")`
   → just stop calling tools, wakeup ends. The new worker runs,
   fetches the pages, writes the file, then `mailbox_send`s leader.
   Auto-wake-on-mail starts a fresh wakeup of leader; it reads the
   worker's reply and mails you. Two wakeups, one mail thread; no
   blocking wait — the runtime is event-driven all the way down.

5. **Verify**:

   ```bash
   ls -la ~/.lyre/memory/facts/research-*.md
   uv run lyre mailbox owner --unread-only
   uv run lyre wakeups list --since 10m
   ```
