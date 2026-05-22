# Lyre — Plugin 体系

> **文档定位**：定义 Lyre 的 plugin 体系——agent 在不修改 `src/lyre/` 的前提下，把行为注入到 runtime 关键位置（system prompt 装配 / tool dispatch / wakeup 生命周期 / mailbox 投递）。设计参考 [pi 的 hooks 设计](https://github.com/earendil-works/pi/blob/main/packages/agent/docs/hooks.md) 与 [Claude Code 的 marketplace 模型](https://anthropic.com/claude-code/marketplace.schema.json)，但坚持 Lyre 的五条铁律。
> **相关**：[`FOUNDATION.md`](./FOUNDATION.md) 五条铁律；[`AGENT_RUNTIME.md`](./AGENT_RUNTIME.md) agent loop 实现；[`AGENT_CONTRACT.md`](./AGENT_CONTRACT.md) 工具契约。

> **English summary**: Plugin system for runtime injection — observe / transform / patch at agent_loop, tool dispatch, and mailbox lifecycle points (Pi-style hooks) + direct registries for tools / outbox kinds / MCP servers. Plugins are user-installed Python packages under `~/.lyre/plugins/<name>/`, fully trusted (owner's machine), discovered + loaded at `lyre serve` startup. NOT for extending personas / skills / channels / model registry — those already have file-drop seams.

---

## 目录

1. [动机](#1-动机)
2. [Plugin 形状（filesystem layout）](#2-plugin-形状filesystem-layout)
3. [发现 + 加载生命周期](#3-发现--加载生命周期)
4. [Hook 子系统（runtime injection 核心）](#4-hook-子系统runtime-injection-核心)
5. [Registry 子系统（registration-style 扩展）](#5-registry-子系统registration-style-扩展)
6. [首批 hook 事件清单](#6-首批-hook-事件清单)
7. [Plugin Context（Facade 集）](#7-plugin-contextfacade-集)
8. [信任模型与错误策略](#8-信任模型与错误策略)
9. [Versioning](#9-versioning)
10. [完整示例：working-hours-aware plugin](#10-完整示例working-hours-aware-plugin)
11. [实现路线图](#11-实现路线图)

---

## 1. 动机

Lyre 当前已经有不少**文件落地式**扩展点：personas (markdown)、skills (markdown)、`[[models]]` (config.toml)、ExternalChannel (Python Protocol)、系统提示 `SYSTEM.md` / 每 persona 的 `APPEND.md`。这些覆盖了"换个角色 / 加个能力声明 / 加个对话渠道"的需求。

**plugin 体系不取代这些**——它解决一类完全不同的需求：**在 runtime 的具体执行路径上插入逻辑**。比如：

| 想做的事 | 当前的窘境 | plugin 解 |
|---|---|---|
| 工作时段感知：白天 owner 在线时 dispatcher 详尽汇报，深夜简洁 | 没有"运行时改 system prompt"机制 | hook `system_prompt_assembly`，transform |
| Token 预算守卫：每日 cap 超了不允许新 wakeup | 没有 wakeup gate 钩子 | hook `wakeup_starting`，return `cancel` |
| 关键 tool（`shell_exec`、`mailbox_send` to non-owner）通过外部 IM 二次确认 | `before_tool_dispatch` 无入口 | hook `before_tool_dispatch`，block + 提示 |
| Blocker 邮件触达 Discord/Slack/SMS（除已有 Lark 外） | 邮件投递无 observer | hook `mail_inserted`，observe |
| 累计每周成本，导出 CSV | 没有跨 wakeup 聚合点 | hook `wakeup_ended` observe + Registry: 加个 `lyre cost weekly` CLI 子命令（暂不实现） |
| 新工具（Notion 同步、Calendar 查询） | 改 `src/lyre/runtime/tools/`，污染主 repo | Registry: tools |
| 注册 MCP server 让 agent 拿到外部能力 | 没有 mcp 接入点 | Registry: mcp servers |

核心区别：**file-drop 是声明式**（数据），**plugin 是命令式**（代码）。Lyre 的五条铁律仍然成立——plugin 不能绕过 mailbox、不能跳过持久化、不能违反 kill-test——但 plugin 可以**观测**和**改写**经过 runtime 关键节点的数据。

设计三原则：

1. **Hook = 事件 + 转换链**。同 Pi：每个事件类型自带 result phantom；handler 按顺序跑，下一个看得到上一个的修改。
2. **Registry ≠ Hook**。tools / outbox kinds / mcp servers 是"我提供一个东西"，不是"我观测一个事件"——直接 dict 注册。
3. **不重新发明声明式扩展**。如果一件事能 file-drop 解决（persona / skill / channel / model entry），就 NOT plugin 的事。

---

## 2. Plugin 形状（filesystem layout）

```
~/.lyre/plugins/<plugin-name>/
├── .lyre-plugin/
│   └── manifest.toml          # 必需：name, version, api_version, summary, author
├── __init__.py                # 必需：plugin 注册入口（register(host) 函数）
├── hooks.py                   # 可选：hook handlers 实现
├── tools.py                   # 可选：自定义 tool（Tool 实例 list）
├── mcp.toml                   # 可选：MCP server 声明
├── prompts/
│   └── *.md                   # 可选：可被 hook 读取的提示片段
├── README.md                  # 推荐
└── pyproject.toml             # 推荐：plugin 自身的依赖锁定
```

manifest.toml 必填字段：

```toml
[plugin]
name = "working-hours-aware"
version = "0.1.0"
api_version = "1"               # Lyre plugin API 版本，详见 §9
summary = "Inject time-of-day context into dispatcher's system prompt."
author = "somainer"

[contributes]
# 声明 plugin 都干了啥。Lyre startup 时用这个做 sanity check
# 和未来的"plugin describe"展示，不会真去校验 Python 代码到底
# 注册了什么。
hooks = ["system_prompt_assembly"]
tools = []
outbox_kinds = []
mcp_servers = []
```

`__init__.py` 入口约定：

```python
# ~/.lyre/plugins/working-hours-aware/__init__.py
from lyre.plugins import PluginHost

def register(host: PluginHost) -> None:
    """Lyre 在 startup 时调用一次。Plugin 在此处把自己挂上 hook 总线 + 把自己的工具塞进 registry。"""
    from .hooks import inject_time_of_day
    host.hooks.on("system_prompt_assembly", inject_time_of_day)
```

**不支持**的 layout：
- 不接受单文件 plugin（必须是目录），以便 manifest 能存放
- 不接受可执行 / 二进制（plugin 是纯 Python；要带二进制就走 MCP server 路径）

---

## 3. 发现 + 加载生命周期

### 3.1 发现

`lyre serve` 启动时扫描 `~/.lyre/plugins/`：

```
for each subdir:
    if .lyre-plugin/manifest.toml exists → candidate
    else → skip (silent)
```

候选 plugin 再通过 `cfg.plugins.enabled`（config.toml 列表）筛选：

```toml
# ~/.lyre/config.toml
[plugins]
enabled = ["working-hours-aware", "cost-tracker"]
```

**默认禁用**。即使 plugin 目录存在，没列在 `enabled` 里就不加载——避免误启用陌生代码。

### 3.2 加载顺序

```
1. Lyre core 初始化 (repos, scheduler, dispatcher, channel_registry…)
2. PluginHost 构造（持有 hooks bus + 各 registry 引用）
3. for each enabled plugin in config order:
     a. importlib.import_module(f"lyre_plugin_{name}")  ← 见 §3.3
     b. module.register(host)
     c. 任何异常 → 记录 + 跳过该 plugin（其它继续）
4. 启动 services（含 channel.run() / enqueuer / scheduler …）
```

**顺序敏感**：multiple plugins 注册同一个 hook 时，handlers 按 plugin 加载顺序（= `cfg.plugins.enabled` 的顺序）触发。这是约定俗成的"列表序就是优先级"——plugin 作者通过文档告诉用户该把自己排前还是排后。

### 3.3 Python import 桥

`~/.lyre/plugins/<name>/` 不在 Python sys.path 里。两种处理：

- **方案 A（采纳）**：lyre 把 `~/.lyre/plugins/` 临时 append 到 `sys.path`，然后 `importlib.import_module(name)`。Plugin 名字直接是顶层包名。简单，但 plugin 名不能撞 stdlib / lyre 自身的模块名。manifest 名字字段强制 `lyre_<...>` 或仅小写字母+下划线，做 sanity check。
- 方案 B（拒绝）：用 `importlib.util.spec_from_file_location` 显式 import。更安全但 plugin 自己 import 其他模块时要折腾路径。

### 3.4 卸载 / Reload

**MVP 不支持 reload**。Plugin 改了要 `lyre serve` 重启。理由：
- Python 没有可靠的"卸下已 import 模块"机制
- Hook handlers 闭包可能持有对 facade 的引用，reload 难追溯
- 信任模型是"重启可控"

`PluginHost.dispose()` 在 daemon 退出时被调用，会按注册顺序逆序跑所有 `addCleanup(...)` 注册的清理函数。

---

## 4. Hook 子系统（runtime injection 核心）

直接借 Pi 的设计——同 [pi/packages/agent/docs/hooks.md](https://github.com/earendil-works/pi/blob/main/packages/agent/docs/hooks.md) 几乎逐字翻译，区别在事件命名 + Lyre 的具体 result 类型。

### 4.1 三种角色

```python
class PluginHooks(Protocol):
    """Plugin 总线。Lyre core 只看这个 Protocol；具体实现注入。"""

    ctx: "PluginContext"     # Facade 集，见 §7

    def observe(self, handler) -> Callable[[], None]:
        """Read-only 观察所有事件。返回 unsubscribe 函数。"""

    def on(self, event_type: str, handler) -> Callable[[], None]:
        """订阅指定事件，handler 的返回值参与该事件的语义。"""

    async def emit(self, event, signal=None):
        """Lyre core 唯一调用入口。按事件类型决定 policy
        （observation / transform / block / patch / first-cancel）。"""

    def add_cleanup(self, cleanup: Callable) -> Callable[[], None]:
        """注册关停清理函数。lyre serve 退出时按逆序运行。"""
```

- `observe()` 看所有事件，**返回值忽略**。不需要知道 event 类型表。适合纯度量、外部通知。
- `on(type)` 参与该事件的语义——返回值会被 emit policy 解释（详见 §4.3 每种事件的策略）。
- `emit()` 由 Lyre core 调用；plugin 不应该自己 emit（约定俗成，没有强制）。
- handlers 可以 `async`，也可以同步函数；emit 都 `await` 它。

### 4.2 事件 + result 类型

每个事件是一个 dataclass，字段里有 `type: str` 字面量，再加上业务字段。事件 result 类型按事件定义（dataclass / TypedDict / None）。

```python
@dataclass
class SystemPromptAssemblyEvent:
    type: Literal["system_prompt_assembly"]
    agent_id: str
    persona_name: str
    fragments: list[str]                # 当前已拼接的 prompt 片段列表

@dataclass
class SystemPromptAssemblyResult:
    """Hook 返回值。None / 缺省 = 不改。"""
    fragments: list[str] | None = None  # 完全替换片段列表
    append: list[str] | None = None     # 追加到末尾（更常见）
```

### 4.3 Per-event policy

不同事件对"多个 handler 之间怎么协作"有不同语义。Lyre 跟 Pi 一样用 **emit policy** 区分：

| Policy | 行为 | 用例 |
|---|---|---|
| **observation** | 全部 handler 并行/串行跑，返回值忽略 | `wakeup_started`、`mail_inserted` |
| **transform** | 串行；handler 返回 `{x=…}` 就替换 event 的 x；下一个 handler 看修改后版本 | `system_prompt_assembly` 的 `append` |
| **block** | 串行；handler 返回 `{block=True, reason=…}` 早退 | `before_tool_dispatch` |
| **patch** | 串行；handler 返回部分字段，累积合并到 result | `after_tool_result` |
| **first-cancel** | 串行；handler 返回 `{cancel=True}` 早退；否则最后一个非 None 结果胜出 | `wakeup_starting`（预算守卫） |

每种 policy 都由 `PluginHooks.emit()` 内部的 switch 实现（参考 Pi 的 `DefaultAgentHarnessHooks.emit`）。Plugin 作者**不需要**知道 policy 名字——他写一个 handler，看到事件类型、返回符合该事件 result 类型的对象即可。Policy 是 emit 端的事。

### 4.4 错误策略

**默认 `continue`**：plugin handler 抛异常 → Lyre 用 `log.exception` 记录（带 plugin 名），跳过该 handler，继续下一个。Lyre core 永远不因 plugin 抛错而崩溃——铁律 3（kill-test）的延伸。

可在 config.toml 切换到 strict：
```toml
[plugins]
error_mode = "strict"   # plugin 抛错 → wakeup 标记 failed
```

观察期建议保持 default。

### 4.5 来源追溯

每个 plugin register 时拿到自己的 **scope**：

```python
def register(host):
    scope = host.hooks.create_scope(source_info={"plugin": "working-hours-aware"})
    scope.on("system_prompt_assembly", my_handler)
```

错误日志、metrics、dashboard 上的 plugin 行为审计都通过 scope 找到来源。Plugin 不主动 `create_scope` 也行——`PluginHost` 会在调 `register()` 时塞个默认 scope 进去。

---

## 5. Registry 子系统（registration-style 扩展）

某些扩展点本质上是"我提供一个 X，Lyre 把它收下"，不需要事件总线。Pi 把这些叫 registry——Lyre 沿用。

| Registry | 内容 | 注册 API |
|---|---|---|
| **tools** | `Tool` 实例（含 name, description, input_schema, handler） | `host.tools.register(my_tool)` |
| **outbox kinds** | (kind_name, async dispatch handler) | `host.outbox.register_kind("my_kind", my_handler)` |
| **mcp servers** | 由 manifest 中的 `mcp.toml` 声明，Lyre 在 startup 拉起 | declarative only — Python 代码不参与 |

### 5.1 Tools registry

Lyre 已经有 `ToolRegistry`（`runtime/tools/__init__.py`）。Plugin host 包装它：

```python
host.tools.register(Tool(
    name="notion_search",
    description="...",
    input_schema={...},
    handler=async_function,   # 同 Lyre 内置 tool 的契约
    allowed_personas=None,    # None = 全部 persona 可调用；list = 限定
))
```

Persona 的 `allowed_lyre_tools` 仍然管控可见性——plugin 注册了 tool ≠ 所有 agent 能用，还要 persona 明确列出。**这是有意为之**：plugin 提供能力，persona 决定哪个角色配用它。

### 5.2 Outbox kinds

```python
async def dispatch_my_kind(row, ctx):
    """同 Lyre 内置 outbox handler 契约。"""
    ...

host.outbox.register_kind("my_kind", dispatch_my_kind)
```

风险高——新 outbox kind 引入新的"什么算 dispatched"逻辑。Plugin 必须保证 idempotent：行 retry 时不能重复副作用。文档+example 必须强调。

### 5.3 MCP servers

manifest 同级目录的 `mcp.toml` 声明 server 启动方式：

```toml
[[mcp_servers]]
name = "weather"
command = "uvx"
args = ["lyre-mcp-weather", "--api-key", "${WEATHER_KEY}"]
env_passthrough = ["WEATHER_KEY"]
```

`lyre serve` startup 后段（registries 注册完，services 启动前）spawn 这些子进程，按 MCP 协议初始化，把它们暴露的 tools 自动注册到 `host.tools`（命名空间前缀 `mcp:<server>:<tool>`）。退出时 SIGTERM 它们。

**纯 declarative**——plugin 不需要 Python 代码就能引入 MCP 工具。

### 5.4 不做的 registries

- **Dashboard 路由 / widget**：复杂度高（HTML / JS / 静态资源），Lyre 自身的 dashboard 已经够用；plugin 想做可视化更适合 emit 数据到外部（Grafana / Prometheus）。
- **Personas / Skills / Channels**：file-drop 已经能做。Plugin 加这个就是重复机制。
- **Custom output styles / message renderers**：dashboard renders 用 Jinja filters，不开放给 plugin。

---

## 6. 首批 hook 事件清单

针对 owner 主诉"long-running，为我工作"，挑选 5 个能撬动最多用例的 hook 点。每个事件给出：触发位置、字段、result 类型、policy、典型用例。

### 6.1 `system_prompt_assembly`

**触发**：`runtime/context.py:assemble_system_prompt()` 返回前。
**Policy**：transform（chain）。
**Event**：
```python
@dataclass
class SystemPromptAssemblyEvent:
    type: Literal["system_prompt_assembly"]
    agent_id: str
    persona_name: str
    fragments: list[str]       # 已组装的片段（identity / persona / APPEND / SYSTEM / agents-dir / memory / skills）
```
**Result**：
```python
class SystemPromptAssemblyResult(TypedDict, total=False):
    fragments: list[str]       # 完全替换
    append: list[str]          # 追加到末尾
```
**典型用例**：时段感知、当日 owner 心情 / 优先级、动态 persona 提示注入。

### 6.2 `wakeup_starting`

**触发**：scheduler 解析 candidates 之后、`agent_loop.run()` 之前。
**Policy**：first-cancel。
**Event**：
```python
@dataclass
class WakeupStartingEvent:
    type: Literal["wakeup_starting"]
    agent_id: str
    task_id: str
    candidates: list[ModelEntry]
```
**Result**：
```python
class WakeupStartingResult(TypedDict, total=False):
    cancel: bool
    reason: str               # 写入 task.last_error，wakeup 标记 silent_close
```
**典型用例**：每日 token 预算守卫、夜间静默时段、人为停一下不重启 daemon。

### 6.3 `before_tool_dispatch`

**触发**：`agent_loop._dispatch_tool()` 入口。
**Policy**：block。
**Event**：
```python
@dataclass
class BeforeToolDispatchEvent:
    type: Literal["before_tool_dispatch"]
    agent_id: str
    task_id: str
    tool_name: str
    tool_input: dict[str, Any]
```
**Result**：
```python
class BeforeToolDispatchResult(TypedDict, total=False):
    block: bool
    reason: str               # 作为 is_error=True 的 tool_result 文本返回给 model
    rewrite_input: dict       # 重写 tool_input（block=False 时生效）
```
**典型用例**：高危 tool（`shell_exec`、`mailbox_send` to non-owner）通过 IM 二次确认；audit 日志；按时段限制工具集。

### 6.4 `after_tool_result`

**触发**：`agent_loop._dispatch_tool()` 拿到 `result` 之后、append 到 message list 之前。
**Policy**：patch（累积）。
**Event**：
```python
@dataclass
class AfterToolResultEvent:
    type: Literal["after_tool_result"]
    agent_id: str
    task_id: str
    tool_name: str
    tool_use_id: str
    tool_input: dict[str, Any]
    tool_result: Any           # 可能是 str / dict / list[ContentBlock]
    is_error: bool
```
**Result**：
```python
class AfterToolResultResult(TypedDict, total=False):
    tool_result: Any           # 覆写 tool_result（注意 None 是有效值；缺省键 = 不改）
    is_error: bool
    note: str                  # 追加一段说明到 result（patch 模式累积）
```
**典型用例**：截断超长 tool output、给可疑结果加警告语、本地缓存重写。

### 6.5 `mail_inserted`

**触发**：`MailboxRepository.insert_message()` 提交事务之后。
**Policy**：observation。
**Event**：
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
**Result**：None。
**典型用例**：blocker 邮件触达 Discord/Telegram/SMS；外部 audit 流；metric 累积。

> **不在首批**：`wakeup_ended`、`task_status_changed`、`scheduler_tick`、`compact_triggered`、`provider_request`、`stream_event_received`。这些是好点子但属于"先看看 plugin 实际用法再开"。每开一个 hook 点都是 API 表面积，覆水难收。

---

## 7. Plugin Context（Facade 集）

`PluginContext` 是 plugin 和 Lyre runtime 之间的唯一接触面——facade 对象集，**不暴露**内部类（Scheduler / AgentLoop 实例不传给 plugin）。这样 src 的内部 refactor 不会带火 plugins。

```python
@dataclass(frozen=True)
class PluginContext:
    """Plugin handler 拿到的接触面。"""

    # 持久层 facade —— 同 runtime tools 用的那个
    repos: "Repositories"

    # blob 存储（如果有）
    blob_store: "BlobStore | None"

    # 外部 channel registry（plugin 可以查"现在 Lark 通不通"等）
    channels: "ChannelRegistry"

    # plugin 自己的元信息
    plugin_name: str
    plugin_version: str

    # 配置入口 —— plugin 可读自己 manifest 之外的 config，
    # 比如 ~/.lyre/config.toml [plugins.<name>] sub-table
    plugin_config: dict[str, Any]

    # 结构化日志（已绑定 plugin_name）
    log: "structlog.BoundLogger"

    # 计划性副作用入口（避免 plugin 直接 repos.outbox.enqueue 绕开 channel_publish 路径）
    def enqueue_owner_mail(
        self, body: str, urgency: str = "normal", title: str | None = None,
    ) -> None: ...
    def enqueue_channel_publish(
        self, msg_id: int, channel: str, reply_to_external_id: str | None = None,
    ) -> None: ...
```

> **关键约束**：plugin 不能拿到 AgentLoop / Scheduler / OutboxDispatcher 实例本身。它们的内部状态属于 Lyre core 的实现细节，暴露出来就锁死了 refactor 自由度。"我要做 X" 的需求必须通过 facade 表达；当前 facade 不支持某种 X 时，**正确的回应是给 PluginContext 加 helper，不是给 plugin 加更广的访问权**。

PluginContext 在每次 `emit()` 时随 event 传给 handler；hook 之外的注册时刻（`register(host)`），host 直接持有 ctx 引用。

---

## 8. 信任模型与错误策略

**全信任**，由 owner 自负其责。

- 没有沙箱、没有权限声明、没有 capability gate。Plugin 拿到 `PluginContext.repos` 就能任意改 SQLite。
- Lyre 默认禁用所有 plugin（`config.toml [plugins] enabled = []`），enable 行为本身就是 owner 的"我信任这个代码"。
- Plugin 代码必须 owner 亲自审；marketplace 在 MVP 不做（社区分发是后续话题）。

**异常隔离**（错误策略）：
- Plugin handler 抛异常 → log + skip，**不传染** Lyre core。详见 §4.4。
- Plugin 在 `register(host)` 阶段抛异常 → 该 plugin 标记失败，其它 plugin 继续；wakeup 不受影响。

**清理**：
- `lyre serve` 退出时调用 `PluginHost.dispose()`，逆序运行所有 `add_cleanup()` 注册的清理函数。
- Plugin 启动的线程 / 子进程 / WebSocket 必须自己用 `add_cleanup` 注册关停——daemon 退出时漏关 = 进程僵尸。

---

## 9. Versioning

```toml
[plugin]
api_version = "1"
```

`api_version` 是 **Lyre plugin API** 的版本号，跟 Lyre 自身版本解耦。规则：

- API breaking change → bump major（"2"），旧 plugin 在新 Lyre 上加载时打 warning 但继续 try；handler 行为可能因事件 schema 变了出错——错误策略兜底。
- 新增 hook / 新增 ctx facade method → 不 bump（向后兼容追加）。
- 重命名 / 删除事件 → bump major + 在 Lyre changelog 写迁移指引。

Lyre core 持有 `SUPPORTED_PLUGIN_API_VERSIONS = {"1"}` 集合；plugin 的 api_version 不在集合里 = 拒绝加载并日志解释。

---

## 10. 完整示例：working-hours-aware plugin

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
"""Working-hours-aware: tells the dispatcher whether the owner is
'awake / likely to reply within 5 min' vs 'sleeping / batch later'."""
from __future__ import annotations
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo

from lyre.plugins import PluginHost, PluginContext
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

**`~/.lyre/config.toml`** 启用：
```toml
[plugins]
enabled = ["working-hours-aware"]
```

下次 `lyre serve` 启动后，dispatcher 每次 wakeup 的 system prompt 末尾会带一段"现在 owner 在不在线"。Plugin 改了立即重启即生效；想关掉就从 `enabled` 列表删除。

---

## 11. 实现路线图

| PR | 范围 | 主要 deliverable |
|---|---|---|
| **PR 1** | 本 spec doc | 设计审 + 锁定 API |
| **PR 2** | PluginHost + Hook bus 框架 | `src/lyre/plugins/__init__.py`、`hooks.py`、PluginContext facade；不接任何真实 lifecycle 点（用 dummy emit 测） |
| **PR 3** | 5 个首批 hook 接入 | 在 `runtime/context.py`、`runtime/agent_loop.py`、`scheduler/scheduler.py`、`persistence/sqlite_impl.py` 插入 `await host.hooks.emit(...)`；hook 不 attach 任何 plugin 时是零成本 |
| **PR 4** | Discovery + 加载链路 | manifest 解析、`importlib` 桥、`[plugins] enabled` 配置、`lyre plugin list` CLI |
| **PR 5** | Tools registry 接入 | plugin-registered tool 通过 `ToolRegistry` 暴露给 agent；`allowed_personas` 收紧 |
| **PR 6** | Outbox kinds registry 接入 | plugin 注册新 kind + handler |
| **PR 7** | MCP server declarative integration | 解析 `mcp.toml`、spawn 子进程、桥接到 `host.tools` |
| **PR 8**+ | Cookbook | 1-2 个 reference plugin 落地：`working-hours-aware`、`tool-call-audit`、`daily-cost-cap` |

每个 PR 各自带测试。PR 之间不强耦合 —— PR 2 + PR 3 + PR 4 是最小可用集（plugin 能跑 hook），PR 5+ 在此之上叠加。

---

## 参考

- [pi hooks design](https://github.com/earendil-works/pi/blob/main/packages/agent/docs/hooks.md) —— `observe / on / emit` 三角色，per-event policy，scope 来源追溯
- [Claude Code plugin marketplace](https://anthropic.com/claude-code/marketplace.schema.json) —— `.claude-plugin/plugin.json` manifest 形状，marketplace 分发
- [Pluggy](https://pluggy.readthedocs.io/) —— Python 端类似系统的成熟实现，可借鉴 hookspec / hookimpl 装饰器模式（暂不采用，理由：再加一层抽象、API 表面积膨胀）
