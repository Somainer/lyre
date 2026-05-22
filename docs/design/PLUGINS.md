# Lyre — Plugin System

> **Scope**: Defines Lyre's plugin system — letting agents extend runtime behavior **without modifying `src/lyre/`**. Designed in the spirit of [pi's hooks design](https://github.com/earendil-works/pi/blob/main/packages/agent/docs/hooks.md) and [Claude Code's marketplace model](https://anthropic.com/claude-code/marketplace.schema.json), while honoring Lyre's five iron laws.
> **See also**: [`FOUNDATION.md`](./FOUNDATION.md) (the five laws), [`AGENT_RUNTIME.md`](./AGENT_RUNTIME.md) (agent loop internals), [`AGENT_CONTRACT.md`](./AGENT_CONTRACT.md) (tool contract).
>
> *Note: existing design docs in this directory are in Chinese for historical reasons. New design docs use English so the project stays approachable to the broader open-source audience.*

---

## Contents

1. [Motivation](#1-motivation)
2. [Plugin layout](#2-plugin-layout)
3. [Discovery + load lifecycle](#3-discovery--load-lifecycle)
4. [The Hooks subsystem](#4-the-hooks-subsystem)
5. [The Registry subsystem](#5-the-registry-subsystem)
6. [First-batch hook events](#6-first-batch-hook-events)
7. [PluginContext (facade set)](#7-plugincontext-facade-set)
8. [Trust model + error policy](#8-trust-model--error-policy)
9. [Versioning](#9-versioning)
10. [Worked example: `working-hours-aware`](#10-worked-example-working-hours-aware)
11. [Implementation roadmap](#11-implementation-roadmap)

---

## 1. Motivation

Lyre already has several **file-drop extension points**: personas (markdown), skills (markdown), `[[models]]` entries (config.toml), `ExternalChannel` implementations (Python Protocol under `src/lyre/integrations/<name>/`), and prompt fragments via `SYSTEM.md` / per-persona `APPEND.md`. These cover "swap a role / declare a capability / add a chat surface".

**The plugin system does not replace those.** It solves a different problem class: **inject logic at specific runtime execution paths**. Examples:

| Goal | Why file-drop can't do it | Plugin solves it via |
|---|---|---|
| Time-of-day awareness — dispatcher reports verbosely during owner's working hours, tersely overnight | No "rewrite system prompt at runtime" mechanism | hook `system_prompt_assembly` (transform) |
| Token budget guard — refuse new wakeups when daily cap exhausted | No wakeup-gating point | hook `wakeup_starting`, return `cancel` |
| High-risk tools (`shell_exec`, `mailbox_send` to non-owner) gated by external IM confirmation | No interception before tool dispatch | hook `before_tool_dispatch`, return `block` |
| Blocker mail surfaced through Discord/Slack/SMS in addition to Lark | No observer on mail insertion | hook `mail_inserted` (observe) |
| Per-week cost rollup, exported to CSV | No cross-wakeup aggregation point | hook `wakeup_ended` + a registered CLI subcommand (deferred) |
| Register an MCP server to give agents external capabilities | No MCP integration point today | registry: `mcp_servers` (declarative) |
| Notify owner / page workers when the system has been idle too long | Scheduler doesn't track idle gaps today | hook `idle_tick` (observe / first-cancel) |

The core distinction: **file-drop is declarative** (data), **plugins are imperative** (code). Lyre's five iron laws still hold — plugins cannot bypass the mailbox, skip persistence, or violate kill-test recoverability — but plugins **can observe and rewrite** data flowing through key runtime nodes.

Three design principles:

1. **Hook = event + transformation chain**, modeled directly on pi: each event type carries its own result-type phantom; handlers run in registration order, each one sees the previous's mutations.
2. **Registry ≠ hook**. `tools` / `outbox_kinds` / `mcp_servers` are "I provide a thing", not "I observe an event". Direct dict registration, no event bus.
3. **Don't reinvent declarative extension**. If the need can be met by dropping a file (persona / skill / channel / model entry), that's NOT what plugins are for.

---

## 2. Plugin layout

```
~/.lyre/plugins/<plugin-name>/
├── .lyre-plugin/
│   └── manifest.toml          # required: name, version, api_version, summary, author
├── __init__.py                # required: register(host) entry point
├── hooks.py                   # optional: hook handler implementations
├── tools.py                   # optional: custom Tool instances
├── mcp.toml                   # optional: MCP server declarations
├── prompts/
│   └── *.md                   # optional: prompt fragments read by hooks
├── README.md                  # recommended
└── pyproject.toml             # recommended: pin the plugin's own deps
```

manifest.toml required fields:

```toml
[plugin]
name = "working-hours-aware"
version = "0.1.0"
api_version = "1"               # Lyre plugin-API version, see §9
summary = "Inject time-of-day context into dispatcher's system prompt."
author = "somainer"

[contributes]
# Self-declaration of what this plugin contributes. Used by Lyre at
# startup for sanity-checking + future "lyre plugin describe" output.
# Lyre does NOT verify the Python code actually registers what's
# declared — this is documentation, not enforcement.
hooks = ["system_prompt_assembly"]
tools = []
outbox_kinds = []
mcp_servers = []
```

`__init__.py` entry point convention:

```python
# ~/.lyre/plugins/working-hours-aware/__init__.py
from lyre.plugins import PluginHost

def register(host: PluginHost) -> None:
    """Called once by Lyre at startup. The plugin attaches its
    handlers to the hook bus and registers any tools / outbox
    kinds into the host's registries here."""
    from .hooks import inject_time_of_day
    host.hooks.on("system_prompt_assembly", inject_time_of_day)
```

**Unsupported layouts**:

- Single-file plugins (must be a directory, so manifest has a home)
- Executables / binaries (plugins are pure Python; bring binaries via MCP servers instead)

---

## 3. Discovery + load lifecycle

### 3.1 Discovery

At `lyre serve` startup, Lyre walks `~/.lyre/plugins/`:

```
for each subdir:
    if .lyre-plugin/manifest.toml exists → candidate
    else → skip (silent)
```

Candidate plugins are then filtered by `cfg.plugins.enabled` (a list in config.toml):

```toml
# ~/.lyre/config.toml
[plugins]
enabled = ["working-hours-aware", "cost-tracker"]
```

**Default-off**: a plugin directory existing on disk is not enough — it must be named explicitly in `enabled`. This makes "I trust this plugin" an active owner decision, not an accident.

### 3.2 Load order

```
1. Lyre core initializes (repos, scheduler, dispatcher, channel_registry, …)
2. PluginHost constructed (holds the hook bus + references to each registry)
3. For each enabled plugin, in config order:
     a. importlib.import_module(name)   ← see §3.3
     b. module.register(host)
     c. Any exception → log + skip this plugin (others continue)
4. Start services (channel.run() / enqueuer / scheduler / outbox / …)
```

**Order-sensitive**: when multiple plugins register handlers on the same hook, they fire in plugin load order (= the order in `cfg.plugins.enabled`). This is the natural "list position is priority" convention — plugin authors document whether their plugin should sit early or late.

### 3.3 Python import bridge

`~/.lyre/plugins/<name>/` is not on Python's `sys.path` by default. Two options:

- **Option A (adopted)**: Lyre prepends `~/.lyre/plugins/` to `sys.path` and then `importlib.import_module(name)`. Simple. The plugin's top-level package name IS its directory name. Tradeoff: plugin names can't collide with stdlib / Lyre's own modules. The manifest's `name` field is validated against a strict pattern (`^[a-z][a-z0-9_]*$`) for this reason.
- Option B (rejected): `importlib.util.spec_from_file_location` with explicit paths. Safer, but the plugin's own `from . import …` gets fiddly and the surprise factor on plugin authors isn't worth it.

### 3.4 Unload / reload

**MVP does not support reload.** Plugin code changes require a `lyre serve` restart. Reasons:

- Python has no reliable "unload an imported module" mechanism
- Hook handlers may close over facade references, making reload's transactional semantics unclear
- The trust model is already "restart-is-fine"

`PluginHost.dispose()` is called on daemon exit and runs every cleanup function registered via `add_cleanup(...)` in reverse registration order.

---

## 4. The Hooks subsystem

Closely mirrored on pi's design — see [pi/packages/agent/docs/hooks.md](https://github.com/earendil-works/pi/blob/main/packages/agent/docs/hooks.md). The differences are event names and Lyre-specific result types.

### 4.1 Three roles

```python
class PluginHooks(Protocol):
    """The plugin event bus. Lyre core only sees this Protocol;
    the concrete implementation is injected."""

    ctx: "PluginContext"     # facade set, see §7

    def observe(self, handler) -> Callable[[], None]:
        """Read-only — handler sees every event but its return
        value is ignored. Returns an unsubscribe callable."""

    def on(self, event_type: str, handler) -> Callable[[], None]:
        """Subscribe to a specific event. The handler's return
        value participates in that event's semantics (see §4.3)."""

    async def emit(self, event, signal=None):
        """Called only by Lyre core. Dispatches by event type
        according to per-event policy: observation / transform /
        block / patch / first-cancel."""

    def add_cleanup(self, cleanup: Callable) -> Callable[[], None]:
        """Register a shutdown-time cleanup. Run in reverse order
        when lyre serve exits."""
```

- `observe()` sees all events, **return value ignored**. Doesn't need to know the event type table. Suited to pure metrics, external notifications.
- `on(type)` participates in that event's semantics — the return value is interpreted by the emit policy (see §4.3 per event).
- `emit()` is for Lyre core; plugins should not call it (convention only, not enforced).
- Handlers may be `async` or sync; `emit` awaits them either way.

### 4.2 Events + result types

Each event is a dataclass with a literal `type` discriminator and business fields. The result type is whatever the event chooses (dataclass / TypedDict / None).

```python
@dataclass
class SystemPromptAssemblyEvent:
    type: Literal["system_prompt_assembly"]
    agent_id: str
    persona_name: str
    fragments: list[str]                # the prompt fragments already assembled

class SystemPromptAssemblyResult(TypedDict, total=False):
    """Hook return value. None / missing keys = no change."""
    fragments: list[str]                # full replacement of the list
    append: list[str]                   # tack on to the end (more common)
```

### 4.3 Per-event policy

Different events have different "how do multiple handlers compose" semantics. Lyre, like pi, uses **emit policy** as the dispatcher:

| Policy | Behavior | Use case |
|---|---|---|
| **observation** | Every handler runs, return values ignored | `wakeup_started`, `mail_inserted` |
| **transform** | Sequential; a handler returning `{x=…}` replaces the event's `x`; the next handler sees the updated value | `system_prompt_assembly`'s `append` |
| **block** | Sequential; a handler returning `{block=True, reason=…}` short-circuits | `before_tool_dispatch` |
| **patch** | Sequential; partial-field returns accumulate into the result | `after_tool_result` |
| **first-cancel** | Sequential; `{cancel=True}` short-circuits; otherwise the last non-None result wins | `wakeup_starting` (budget guard) |

Each policy is implemented inside `PluginHooks.emit()`'s switch (cf. pi's `DefaultAgentHarnessHooks.emit`). Plugin authors **don't need to know the policy names** — they write a handler, see the event type, return a value matching the event's documented result type, and the bus takes care of the rest. Policy is the emit side's concern.

### 4.4 Error policy

**Default `continue`**: a handler that raises → Lyre logs it via `log.exception` (tagged with the plugin's name) and skips that handler. The next handler in the chain runs. Lyre core never crashes because of plugin code — an extension of iron law 3 (kill-test).

Switchable in config:

```toml
[plugins]
error_mode = "strict"     # plugin raise → wakeup marked failed
```

Default-continue is recommended during shake-out.

### 4.5 Source attribution

When a plugin registers, it gets its own **scope**:

```python
def register(host):
    scope = host.hooks.create_scope(source_info={"plugin": "working-hours-aware"})
    scope.on("system_prompt_assembly", my_handler)
```

Error logs, metrics, and any future dashboard audit of plugin behavior trace back to the scope's `source_info`. If a plugin doesn't call `create_scope` explicitly, `PluginHost` wraps a default scope around its `register(host)` call automatically — no plugin author has to opt in for traceability.

---

## 5. The Registry subsystem

Some extension points are "I provide an X — please use it", not "I observe an event". Pi calls these registries; Lyre adopts the term.

| Registry | What | Registration API |
|---|---|---|
| **outbox kinds** | (kind_name, async dispatch handler) | `host.outbox.register_kind("my_kind", my_handler)` |
| **mcp_servers** | Declared in manifest-adjacent `mcp.toml`; Lyre spawns them at startup | declarative only — no Python registration |

> **Tools registry is intentionally absent.** Lyre's agents already have `python_exec` and `shell_exec` as general-purpose escape hatches — "Everything is Python or Bash" is a deliberate stance. Adding a Pi-style tools registry would mostly create a second way to do the same thing the agent can already do via Python, while introducing a confusing trust split (plugin author vs. persona author vs. owner). The right path for adding **structured external capabilities** is the MCP server registry below; the right path for adding **Lyre-internal coordination tools** (mailbox, dispatch, memory) is built-in tool development, not plugins.

### 5.1 Outbox kinds

```python
async def dispatch_my_kind(row, ctx):
    """Same contract as Lyre's built-in outbox handlers."""
    ...

host.outbox.register_kind("my_kind", dispatch_my_kind)
```

High-risk — a new outbox kind introduces a new notion of "what counts as dispatched". The plugin must guarantee idempotence: a row retry must not double-fire the side effect. Documentation + examples must hammer this home.

### 5.2 MCP servers

The manifest's adjacent `mcp.toml` declares server invocations:

```toml
[[mcp_servers]]
name = "weather"
command = "uvx"
args = ["lyre-mcp-weather", "--api-key", "${WEATHER_KEY}"]
env_passthrough = ["WEATHER_KEY"]
```

After registries are populated and before services start, `lyre serve` spawns these subprocesses, initializes them per the MCP protocol, and auto-registers their exposed tools into Lyre's built-in `ToolRegistry` namespaced as `mcp:<server>:<tool>`. At shutdown, they're SIGTERM'd.

Whether any given agent can use `mcp:<server>:<tool>` is then controlled by that agent's persona `allowed_lyre_tools` — the same gate that controls built-in tools. Plugins don't carry per-tool visibility metadata; persona allowlist is the single source of "who can call what".

**Pure declarative** — a plugin can introduce MCP-backed tools without writing any Python.

### 5.3 What's deliberately NOT a registry

- **Built-in agent tools (typed `Tool` instances)**: as noted above, redundant with `python_exec` / `shell_exec`. Use MCP servers for structured external capabilities; use built-in tool development for Lyre-internal coordination.
- **Dashboard routes / widgets**: complexity is high (HTML / JS / static assets); Lyre's own dashboard is already adequate; plugins that want visualization should emit data to external systems (Grafana / Prometheus / similar).
- **Personas / Skills / Channels**: file-drop already works. Adding plugin-side registration would duplicate the mechanism.
- **Custom output styles / message renderers**: the dashboard's Jinja filters aren't exposed to plugins.

---

## 6. First-batch hook events

For the "long-running, working for me overnight" use case, six hook points cover the highest-value extension scenarios. Each entry below lists: trigger location, fields, result type, policy, typical uses.

### 6.1 `system_prompt_assembly`

**Trigger**: `runtime/context.py:assemble_system_prompt()` just before the return.
**Policy**: transform (chain).
**Event**:
```python
@dataclass
class SystemPromptAssemblyEvent:
    type: Literal["system_prompt_assembly"]
    agent_id: str
    persona_name: str
    fragments: list[str]       # the assembled fragments (identity / persona / APPEND / SYSTEM / agents-directory / memory / skills)
```
**Result**:
```python
class SystemPromptAssemblyResult(TypedDict, total=False):
    fragments: list[str]       # full replacement
    append: list[str]          # tack on to the end
```
**Use cases**: time-of-day context, owner mood / priority, dynamic persona-specific reminders.

### 6.2 `wakeup_starting`

**Trigger**: scheduler has resolved candidates, before `agent_loop.run()`.
**Policy**: first-cancel.
**Event**:
```python
@dataclass
class WakeupStartingEvent:
    type: Literal["wakeup_starting"]
    agent_id: str
    task_id: str
    candidates: list[ModelEntry]
```
**Result**:
```python
class WakeupStartingResult(TypedDict, total=False):
    cancel: bool
    reason: str                # written to task.last_error; wakeup marked silent_close
```
**Use cases**: daily token budget guard, quiet hours, "pause everything without restarting the daemon".

### 6.3 `before_tool_dispatch`

**Trigger**: `agent_loop._dispatch_tool()` entry.
**Policy**: block.
**Event**:
```python
@dataclass
class BeforeToolDispatchEvent:
    type: Literal["before_tool_dispatch"]
    agent_id: str
    task_id: str
    tool_name: str
    tool_input: dict[str, Any]
```
**Result**:
```python
class BeforeToolDispatchResult(TypedDict, total=False):
    block: bool
    reason: str                # returned to the model as the tool_result text (with is_error=True)
    rewrite_input: dict        # rewrite tool_input (only honored when block=False)
```
**Use cases**: high-risk tools (`shell_exec`, `mailbox_send` to non-owner) gated by external IM confirmation; audit logging; time-of-day restrictions on toolset.

### 6.4 `after_tool_result`

**Trigger**: `agent_loop._dispatch_tool()` has the `result` in hand, before appending it to the message list.
**Policy**: patch (accumulating).
**Event**:
```python
@dataclass
class AfterToolResultEvent:
    type: Literal["after_tool_result"]
    agent_id: str
    task_id: str
    tool_name: str
    tool_use_id: str
    tool_input: dict[str, Any]
    tool_result: Any           # may be str / dict / list[ContentBlock]
    is_error: bool
```
**Result**:
```python
class AfterToolResultResult(TypedDict, total=False):
    tool_result: Any           # overwrite tool_result (None is a valid value; missing key = no change)
    is_error: bool
    note: str                  # append a note to result (patch-accumulated)
```
**Use cases**: truncate over-long tool output, annotate suspicious results, local caching rewrites.

### 6.5 `mail_inserted`

**Trigger**: `MailboxRepository.insert_message()` after the transaction commits.
**Policy**: observation.
**Event**:
```python
@dataclass
class MailInsertedEvent:
    type: Literal["mail_inserted"]
    msg_id: int
    recipient: str
    sender: str
    urgency: Literal["blocker", "high", "normal", "low"]
    title: str | None
```
**Result**: None.
**Use cases**: blocker mail surfaced to Discord/Telegram/SMS; external audit stream; metric counters.

### 6.6 `idle_tick`

**Trigger**: scheduler's idle-watchdog phase, when the runtime has gone N consecutive ticks without starting a wakeup AND there are unread mails OR pending tasks in the system. Default N → roughly 5 minutes of wall-clock silence on a 1s poll interval; configurable.

**Policy**: observation (primary) + first-cancel optional override.

**Event**:
```python
@dataclass
class IdleTickEvent:
    type: Literal["idle_tick"]
    idle_for_seconds: float
    unread_mail_count: int
    pending_task_count: int
    needs_input_task_count: int      # tasks awaiting owner / parent input
    last_wakeup_at: str | None       # ISO8601, None if no wakeup ever
```

**Result**:
```python
class IdleTickResult(TypedDict, total=False):
    suppress_default_nudge: bool     # tell scheduler NOT to auto-nudge dispatcher this tick
                                     # — plugin will handle it (push notification / Lark ping / etc.)
```

**Use cases**:

- **Why this exists**: the most-reported real-world Lyre failure is "dispatcher decided not to dispatch, system goes blank, owner doesn't realize until hours later". Scheduler core gets a watchdog phase that nudges the dispatcher with a synthetic blocker mail when this state persists — and `idle_tick` is the plugin hook on the same path so plugins can customize how the nudge surfaces:
  - Push a notification to owner's phone via existing Lark/Discord channel
  - Pause the nudge during quiet hours (return `suppress_default_nudge=True` after sending its own deferred message)
  - Aggregate metrics: "system was idle X% of yesterday"
- The hook never **prevents** the watchdog from firing — Lyre's invariant is "system should be making progress when there's work pending". `suppress_default_nudge` only tells the watchdog "I have it; don't double-page". Returning nothing leaves the default behavior intact.

> **Implementation note**: the scheduler-side idle watchdog itself is independent infrastructure — it ships even if no plugin attaches to `idle_tick`. The hook is for *customizing* an already-correct default, not for *replacing* a missing one.

> **Not in the first batch**: `wakeup_ended`, `task_status_changed`, `scheduler_tick`, `compact_triggered`, `provider_request`, `stream_event_received`, `subagent_completed`, `scheduled_mail_due`. These are good ideas but fall into "let's see what plugins actually need first". Every new hook is API surface — once exposed, hard to take back.

---

## 7. PluginContext (facade set)

`PluginContext` is the **only** interface between a plugin and Lyre's runtime — a frozen facade set. Internal classes (Scheduler / AgentLoop / OutboxDispatcher) are **not** passed to plugins. This way, refactors inside `src/lyre/` don't unilaterally break plugins.

```python
@dataclass(frozen=True)
class PluginContext:
    """What plugin handlers see. Stable across Lyre versions within
    one major plugin-API version."""

    # Persistence facade — same one runtime tools use.
    repos: "Repositories"

    # Blob storage (multimodal). None if not configured.
    blob_store: "BlobStore | None"

    # External channel registry — plugin can ask "is Lark currently
    # available?" etc.
    channels: "ChannelRegistry"

    # The plugin's own metadata
    plugin_name: str
    plugin_version: str

    # Config sub-table for this plugin — `[plugins.<name>]` in
    # config.toml gets parsed and passed here.
    plugin_config: dict[str, Any]

    # Structured logger, pre-bound with plugin_name.
    log: "structlog.BoundLogger"

    # Side-effect helpers — these avoid plugins reaching into
    # repos.outbox.enqueue() directly and bypassing the
    # channel_publish path.
    def enqueue_owner_mail(
        self, body: str, urgency: str = "normal", title: str | None = None,
    ) -> None: ...
    def enqueue_channel_publish(
        self, msg_id: int, channel: str, reply_to_external_id: str | None = None,
    ) -> None: ...
```

> **The hard constraint**: plugins do not receive AgentLoop / Scheduler / OutboxDispatcher instances. Their internal state is implementation detail; exposing them would lock us out of refactoring. When a plugin needs a new operation, the right response is **"add a helper to PluginContext"** — not "give plugins broader reflection access".

PluginContext is passed alongside the event in every `emit()` call; outside hooks, the `PluginHost` holds a single shared `ctx` reference handed to `register(host)`.

---

## 8. Trust model + error policy

**Full trust**, owner-responsible.

- No sandbox, no permission declarations, no capability gating. Once a plugin has `PluginContext.repos`, it can do anything to SQLite.
- Lyre disables every plugin by default (`config.toml [plugins] enabled = []`). Adding a name to `enabled` is the owner's "I trust this code" act.
- Plugin code must be reviewed by the owner. A marketplace for community distribution is out of scope for MVP.

**Exception isolation** (error strategy):

- Handler raises → log + skip; Lyre core does NOT propagate. See §4.4.
- Plugin raises during `register(host)` → that plugin is marked failed for the session; other plugins continue; wakeups unaffected.

**Cleanup**:

- On `lyre serve` exit, `PluginHost.dispose()` runs every cleanup registered via `add_cleanup()` in reverse order.
- Plugins that spawn threads / subprocesses / WebSockets MUST register their shutdown via `add_cleanup` — otherwise daemon exit leaves zombies.

---

## 9. Versioning

```toml
[plugin]
api_version = "1"
```

`api_version` is the **Lyre plugin API** version, decoupled from Lyre's own version. Rules:

- API breaking change → major bump (`"2"`). Older plugins loaded against newer Lyre log a warning but still try to run; handler behavior may break because event schemas shifted — error policy catches it.
- Adding a new hook / new ctx facade method → no bump (backward-compatible additions).
- Renaming / removing an event → major bump + migration guidance in Lyre's changelog.

Lyre core holds `SUPPORTED_PLUGIN_API_VERSIONS = {"1"}` (a set). A plugin whose `api_version` is not in the set is refused at load time with an explanatory log.

---

## 10. Worked example: `working-hours-aware`

```
~/.lyre/plugins/working-hours-aware/
├── .lyre-plugin/manifest.toml
├── __init__.py
└── README.md
```

**manifest.toml**

```toml
[plugin]
name = "working-hours-aware"
version = "0.1.0"
api_version = "1"
summary = "Inject time-of-day context into the dispatcher's system prompt."
author = "somainer"

[contributes]
hooks = ["system_prompt_assembly"]
```

**`__init__.py`**

```python
"""Working-hours-aware — tells the dispatcher whether the owner is
'awake / likely to reply within 5 min' vs 'sleeping / batch later'."""
from __future__ import annotations
from datetime import datetime
from zoneinfo import ZoneInfo

from lyre.plugins import PluginContext, PluginHost
from lyre.plugins.events import (
    SystemPromptAssemblyEvent,
    SystemPromptAssemblyResult,
)

TZ = ZoneInfo("Asia/Shanghai")


def _phase(now: datetime) -> tuple[str, str]:
    h = now.hour
    if 8 <= h < 19:
        return "awake", "owner is typically awake and replies within 5 min"
    if 19 <= h < 24:
        return "evening", "owner is winding down; replies in 30-60 min"
    return "night", "owner is asleep; batch non-urgent items until morning"


async def on_system_prompt(
    event: SystemPromptAssemblyEvent, ctx: PluginContext,
) -> SystemPromptAssemblyResult | None:
    # Only adjust the dispatcher — workers don't need this context.
    if event.persona_name != "dispatcher":
        return None
    phase, hint = _phase(datetime.now(TZ))
    fragment = (
        f"## Owner activity context\n\n"
        f"Local time {datetime.now(TZ):%Y-%m-%d %H:%M %Z} — phase: **{phase}**.\n"
        f"{hint}\n"
    )
    ctx.log.debug("injected_phase_context", phase=phase)
    return SystemPromptAssemblyResult(append=[fragment])


def register(host: PluginHost) -> None:
    host.hooks.on("system_prompt_assembly", on_system_prompt)
```

Enable in `~/.lyre/config.toml`:

```toml
[plugins]
enabled = ["working-hours-aware"]
```

After the next `lyre serve` restart, every dispatcher wakeup's system prompt ends with an "owner is awake / evening / night" hint. Disable by removing the name from `enabled`.

---

## 11. Implementation roadmap

| PR | Scope | Deliverable |
|---|---|---|
| **PR 1** | This spec doc | Design review + API freeze |
| **PR 2** | PluginHost + hook bus framework | `src/lyre/plugins/__init__.py`, `hooks.py`, `PluginContext` facade. No real lifecycle wiring yet (dummy `emit` tested in isolation). |
| **PR 3** | Wire 5 of the 6 first-batch hook points | `await host.hooks.emit(…)` inserted into `runtime/context.py`, `runtime/agent_loop.py`, `persistence/sqlite_impl.py`. Zero-cost when no plugin attaches. Excludes `idle_tick`, which ships with PR 4. |
| **PR 4** | Scheduler idle watchdog + `idle_tick` hook | New phase in scheduler: detects "no wakeup for N minutes + pending work", auto-nudges dispatcher with synthetic blocker mail. Emits `idle_tick` for plugin customization. Standalone improvement to Lyre regardless of plugin uptake — fixes the "dispatcher forgot to dispatch, system goes blank" failure. |
| **PR 5** | Discovery + load chain | Manifest parsing, `importlib` bridge, `[plugins] enabled` config, `lyre plugin list` CLI. After this PR, plugins are fully usable for hook-based extension. |
| **PR 6** | Outbox kinds registry hookup | Plugin can register a new kind + its dispatch handler. |
| **PR 7** | MCP server declarative integration | Parse `mcp.toml`, spawn subprocesses, bridge their tools into the built-in `ToolRegistry` namespaced as `mcp:<server>:<tool>`. |
| **PR 8**+ | Cookbook | 1-2 reference plugins: `working-hours-aware`, `tool-call-audit`, `idle-pager`. |

Each PR carries its own tests. PR 2 + PR 3 + PR 5 are the minimum useful set (plugins can register hooks); PR 4 is independently useful (fixes real Lyre failure mode); PR 6 / 7 stack independently on top.

---

## References

- [pi hooks design](https://github.com/earendil-works/pi/blob/main/packages/agent/docs/hooks.md) — `observe / on / emit` triad, per-event policy, scope-based source attribution
- [Claude Code plugin marketplace](https://anthropic.com/claude-code/marketplace.schema.json) — `.claude-plugin/plugin.json` manifest shape, marketplace distribution
- [Pluggy](https://pluggy.readthedocs.io/) — Python-ecosystem reference for hookspec / hookimpl decorators. Not adopted: another abstraction layer and a bigger API surface for limited additional value.
