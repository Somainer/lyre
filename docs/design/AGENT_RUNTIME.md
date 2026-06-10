# Lyre — Agent 运行时

> **文档定位**：定义 agent subprocess 内部的运行时实现——LLMAdapter 抽象、provider 适配、agent loop（asyncio + streaming + mid-loop 中断）、MCP server 暴露 Lyre 工具的方式、context 装配、失败处理、计量与 transcript 写入。本文档与 AGENT_CONTRACT 的关系：AGENT_CONTRACT 定**外部接口**（输入输出 / 工具 / 持久层接触面），本文档定**内部实现机制**。
> **相关**：[`FOUNDATION.md`](./FOUNDATION.md) 五条铁律；[`AGENT_CONTRACT.md`](./AGENT_CONTRACT.md) 接口契约；[`TRANSACTION_BOUNDARIES.md`](./TRANSACTION_BOUNDARIES.md) 事务边界；[`PERSISTENCE_SCHEMA.md`](./PERSISTENCE_SCHEMA.md) 持久层。
>
> **Status (2026-06-10)**: split verdict — trust by section. **Current**: §1 (the `LLMAdapter` seam), §3.6–§3.8, §5.5. **Historical (v0.x plan, see the section notes)**: §2 ("唯一实现" is false — three adapters ship), §3.1–§3.5 pseudocode, §4 (MCP gateway, never built). As-built runtime: `RUNTIME_CURRENT.md` (living).

---

## 目录

1. [Provider adapter 抽象（LLMAdapter Protocol）](#1-provider-adapter-抽象llmadapter-protocol)
2. [AnthropicAdapter（MVP 唯一实现）](#2-anthropicadaptermvp-唯一实现)
3. [Agent loop（asyncio + streaming + mid-loop 中断）](#3-agent-loopasyncio--streaming--mid-loop-中断)
4. [Lyre MCP server（工具暴露层）](#4-lyre-mcp-server工具暴露层)
5. [Context 装配](#5-context-装配)
6. [Failure 与 retry](#6-failure-与-retry)
7. [Metering 与 transcript 写入](#7-metering-与-transcript-写入)
8. [未来扩展（OpenAIAdapter / Claude Code via MCP / 其它）](#8-未来扩展openaiadapter--claude-code-via-mcp--其它)
9. [v0.1 已识别但待解决的问题](#9-v01-已识别但待解决的问题)

---

## 1. Provider adapter 抽象（LLMAdapter Protocol）

> 铁律一（Provider 中立）的内部实现层。Lyre 业务代码只看 `LLMAdapter` Protocol，不直接调任何 provider SDK。

### 1.1 内部统一类型

```python
from dataclasses import dataclass
from typing import Literal, AsyncIterator, Any

@dataclass
class LyreContentBlock:
    type: Literal["text", "tool_use", "tool_result"]
    text: str | None = None
    tool_use_id: str | None = None
    tool_name: str | None = None
    tool_input: dict | None = None
    tool_result: Any | None = None
    is_error: bool = False                  # tool_result 时用

@dataclass
class LyreMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: list[LyreContentBlock]

@dataclass
class LyreToolSpec:
    name: str
    description: str
    input_schema: dict                      # JSON Schema (MCP-shape，与 Anthropic input_schema 命名一致)

# Stream event 跨 provider 统一类型
@dataclass
class StreamEvent: ...

@dataclass
class ContentDelta(StreamEvent):
    text: str

@dataclass
class ToolUseStart(StreamEvent):
    id: str
    name: str

@dataclass
class ToolUseDelta(StreamEvent):
    id: str
    input_partial: str                      # 部分 provider 渐进式出 input，其它一次性

@dataclass
class ToolUseComplete(StreamEvent):
    id: str
    name: str
    input: dict

@dataclass
class TurnComplete(StreamEvent):
    stop_reason: Literal["end_turn", "tool_use", "max_tokens", "cancelled", "error"]

@dataclass
class Usage(StreamEvent):
    input_tokens: int
    output_tokens: int

@dataclass
class StreamError(StreamEvent):
    error_kind: Literal["api_error", "timeout", "rate_limit", "cancelled"]
    detail: str
```

### 1.2 LLMAdapter Protocol

```python
class LLMAdapter(Protocol):
    """Provider 无关的 LLM 接口"""

    async def stream_turn(
        self,
        messages: list[LyreMessage],
        tools: list[LyreToolSpec],
        model: str,
        max_tokens: int = 4096,
        temperature: float | None = None,
        system: str | None = None,          # system prompt
    ) -> AsyncIterator[StreamEvent]:
        """流式跑一轮对话；yield 标准化 StreamEvent。
        调用方可在任意时刻中断（generator close / asyncio cancel）。"""
        ...
```

### 1.3 选型理由

- **Stream-first**：所有调用走 stream（行业标配；MVP 默认）
- **Unified event types**：每个 adapter 把 provider 特有 stream protocol 标准化成 `StreamEvent`
- **不假设 MCP**：MCP 是 Lyre 工具的暴露 protocol，但 `LLMAdapter` 跟 MCP 解耦——adapter 用 provider 原生 function-calling protocol 接 LLM，不强制 LLM 走 MCP
- **base_url 配置化**：每个 adapter 应支持 `base_url` 参数（覆盖默认 endpoint），免费拿到"指向 LiteLLM proxy / 自托管 endpoint / Vertex AI / Bedrock / 测试 fake server"等灵活性

---

## 2. AnthropicAdapter（MVP 唯一实现）

> **实现修正 (2026-06-10)**: "唯一实现" is no longer true — `src/lyre/adapter/` ships `anthropic.py`, `openai.py` (chat-completions dialect) and `openai_responses.py` (Responses dialect, selected via the registry entry's `endpoint.api`); the adapter is constructed per model-registry entry in `runtime/adapter_factory.py`. The sketch below is illustrative, not the shipped code — the real adapter also handles thinking blocks and stashes the real `stop_reason` from `message_delta` (the naive mapping below loses it).

### 2.1 实现要点

```python
from anthropic import AsyncAnthropic
from anthropic.types import ...

class AnthropicAdapter:
    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,        # 覆盖默认 https://api.anthropic.com
        timeout: float = 600.0,
    ):
        self.client = AsyncAnthropic(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )

    async def stream_turn(self, messages, tools, model, max_tokens=4096, 
                          temperature=None, system=None):
        anth_messages = self._lyre_to_anthropic_messages(messages)
        anth_tools = [self._lyre_to_anthropic_tool(t) for t in tools]
        
        kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": anth_messages,
            "tools": anth_tools,
        }
        if system: kwargs["system"] = system
        if temperature is not None: kwargs["temperature"] = temperature

        async with self.client.messages.stream(**kwargs) as stream:
            async for event in stream:
                lyre_event = self._anthropic_event_to_lyre(event)
                if lyre_event is not None:
                    yield lyre_event
```

### 2.2 转换映射

**Lyre tool → Anthropic tool**（命名差异小）：

```python
def _lyre_to_anthropic_tool(self, t: LyreToolSpec) -> dict:
    return {
        "name": t.name,
        "description": t.description,
        "input_schema": t.input_schema,     # Anthropic 与 Lyre 都叫 input_schema
    }
```

**Lyre message → Anthropic message**：

```python
def _lyre_to_anthropic_messages(self, msgs: list[LyreMessage]) -> list[dict]:
    out = []
    for m in msgs:
        if m.role == "system":
            continue                        # Anthropic system prompt 单独传，不在 messages 里
        anth_content = []
        for blk in m.content:
            if blk.type == "text":
                anth_content.append({"type": "text", "text": blk.text})
            elif blk.type == "tool_use":
                anth_content.append({
                    "type": "tool_use",
                    "id": blk.tool_use_id,
                    "name": blk.tool_name,
                    "input": blk.tool_input,
                })
            elif blk.type == "tool_result":
                anth_content.append({
                    "type": "tool_result",
                    "tool_use_id": blk.tool_use_id,
                    "content": str(blk.tool_result) if not isinstance(blk.tool_result, list) else blk.tool_result,
                    "is_error": blk.is_error,
                })
        out.append({"role": m.role, "content": anth_content})
    return out
```

**Anthropic event → StreamEvent**：

```python
def _anthropic_event_to_lyre(self, evt) -> StreamEvent | None:
    # Anthropic 的 stream event 类型见官方 SDK 文档
    # MessageStreamEvent 子类包括：
    #   MessageStartEvent / MessageDeltaEvent / MessageStopEvent
    #   ContentBlockStartEvent / ContentBlockDeltaEvent / ContentBlockStopEvent
    
    if isinstance(evt, ContentBlockStartEvent):
        if evt.content_block.type == "tool_use":
            return ToolUseStart(id=evt.content_block.id, name=evt.content_block.name)
        return None  # text block start，不需要 emit
    
    if isinstance(evt, ContentBlockDeltaEvent):
        if evt.delta.type == "text_delta":
            return ContentDelta(text=evt.delta.text)
        if evt.delta.type == "input_json_delta":
            return ToolUseDelta(id=..., input_partial=evt.delta.partial_json)
    
    if isinstance(evt, ContentBlockStopEvent):
        if hasattr(evt.content_block, 'input'):  # tool_use 完成
            return ToolUseComplete(
                id=evt.content_block.id,
                name=evt.content_block.name,
                input=evt.content_block.input,
            )
    
    if isinstance(evt, MessageStopEvent):
        return TurnComplete(stop_reason=evt.message.stop_reason)
    
    if isinstance(evt, MessageDeltaEvent) and evt.usage:
        return Usage(
            input_tokens=evt.usage.input_tokens,
            output_tokens=evt.usage.output_tokens,
        )
    
    return None
```

### 2.3 base_url 配置场景

| 场景 | base_url 设置 |
|---|---|
| Anthropic 官方 | 默认 `https://api.anthropic.com` |
| Anthropic Vertex AI | 用 `AnthropicVertex` 类（同 SDK 提供，API 一致）|
| Anthropic Bedrock | 用 `AnthropicBedrock` 类 |
| LiteLLM proxy | `http://localhost:4000`（LiteLLM 提供 Anthropic-compatible endpoint） |
| OpenRouter Anthropic endpoint | `https://openrouter.ai/api/v1`（如有 Anthropic-compatible 路径） |
| 本地 Anthropic-compatible 服务（如某些 vLLM 配置）| 本地 URL |
| 测试 fake server | `http://localhost:8083` |

Adapter 实现一份代码全部覆盖；选择由 owner 的 config 决定，跟 persona `model_routing` 字段配合：

```json
{
  "primary": {
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
    "base_url": null,
    "api_key_env": "ANTHROPIC_API_KEY"
  },
  "fallback": [
    {"provider": "anthropic", "model": "claude-opus-4-6", "base_url": "https://litellm.local"}
  ]
}
```

---

## 3. Agent loop（asyncio + streaming + mid-loop 中断）

> **实现修正 (2026-06-10)**: the §3.1–§3.5 pseudocode is the v0.x sketch; the shipped loop (`src/lyre/runtime/agent_loop.py`) differs: `max_turns` defaults to **24**, not 50 (per-task override via `tier_overrides`; §6's "max_iterations=50" row is likewise stale). Interrupts are MailWatcher-signal based, not a cancel-and-discard `asyncio.wait` race: `blocker` mail breaks the stream gracefully **keeping the partial assistant turn** and injects a notice as the next user message; `high` mail injects at the turn boundary only. Each turn runs over a **ranked candidate list** with per-turn fallback + circuit breaker (`_run_one_turn_with_fallback` + HealthTracker), not a single adapter. There is no `_should_checkpoint` — checkpoints are agent-driven via `report_progress`. The loop also carries mechanisms not sketched here: silent-turn nudge, dead-loop guard (H1), cooperative stop seam (S0: cancel / wall budget / lease loss), auto-compaction. Exit condition: §3.6 below is authoritative; **§3.6–§3.8 are current**.

### 3.1 主 loop 结构

```python
class AgentLoop:
    def __init__(self, adapter: LLMAdapter, mailbox: MailboxHandle, 
                 transcript: TranscriptWriter, max_iterations: int = 50):
        self.adapter = adapter
        self.mailbox = mailbox
        self.transcript = transcript
        self.max_iterations = max_iterations

    async def run(self, initial_messages: list[LyreMessage], 
                  tools: list[LyreToolSpec],
                  model: str, system: str | None) -> AgentOutput:
        messages = list(initial_messages)
        usage_acc = {"input": 0, "output": 0}
        
        for iteration in range(self.max_iterations):
            # 并行：LLM stream + mailbox blocker watcher
            gen_task = asyncio.create_task(
                self._collect_turn(messages, tools, model, system)
            )
            watch_task = asyncio.create_task(
                self._wait_for_blocker()
            )
            done_set, _ = await asyncio.wait(
                [gen_task, watch_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            
            if watch_task in done_set and not gen_task.done():
                # Owner blocker 到达：cancel LLM
                blocker = watch_task.result()
                gen_task.cancel()
                try:
                    await gen_task
                except asyncio.CancelledError:
                    pass
                # 注入中断到下次 user message
                messages.append(LyreMessage(
                    role="user",
                    content=[LyreContentBlock(
                        type="text",
                        text=f"[OWNER INTERRUPTED]\nFrom: {blocker.sender}\n"
                             f"Urgency: {blocker.urgency}\nBody: {blocker.body}\n\n"
                             f"前面那一轮已被取消。请考虑此中断后继续。",
                    )],
                ))
                self.transcript.note(f"interrupted by blocker msg_id={blocker.id}")
                continue  # 下一轮 LLM 调用
            
            # LLM 正常结束
            watch_task.cancel()
            turn = gen_task.result()
            messages.append(turn.assistant_message)
            usage_acc["input"] += turn.usage_input
            usage_acc["output"] += turn.usage_output
            
            if not turn.tool_calls:
                break  # agent 决定结束
            
            # Dispatch tool calls
            for tc in turn.tool_calls:
                result = await self._dispatch_tool(tc)
                messages.append(LyreMessage(
                    role="user",                # Anthropic 把 tool_result 放 user role
                    content=[LyreContentBlock(
                        type="tool_result",
                        tool_use_id=tc.id,
                        tool_result=result.content,
                        is_error=result.is_error,
                    )],
                ))
            
            # 检查是否到 commit point（每 N 轮 或 任务到关键步骤）
            if self._should_checkpoint(iteration):
                await self._save_progress()
        else:
            # 跑完 max_iterations 还没结束
            return AgentOutput(status="needs_continuation", ...)
        
        return AgentOutput(status="completed", ..., metering=usage_acc)
```

### 3.2 `_collect_turn` 内部

```python
async def _collect_turn(self, messages, tools, model, system) -> Turn:
    """跑一次 stream，收集成 Turn 结构。中途持续写 transcript。"""
    assistant_content: list[LyreContentBlock] = []
    pending_tool_use: dict[str, dict] = {}  # id → partial state
    usage_input = 0
    usage_output = 0
    stop_reason = None
    text_buffer = ""
    
    async for evt in self.adapter.stream_turn(messages, tools, model, system=system):
        if isinstance(evt, ContentDelta):
            text_buffer += evt.text
            self.transcript.write_delta(evt.text)
        elif isinstance(evt, ToolUseStart):
            pending_tool_use[evt.id] = {"name": evt.name, "input_partial": ""}
        elif isinstance(evt, ToolUseComplete):
            assistant_content.append(LyreContentBlock(
                type="tool_use",
                tool_use_id=evt.id,
                tool_name=evt.name,
                tool_input=evt.input,
            ))
            self.transcript.write_tool_use(evt.id, evt.name, evt.input)
        elif isinstance(evt, TurnComplete):
            stop_reason = evt.stop_reason
        elif isinstance(evt, Usage):
            usage_input = evt.input_tokens
            usage_output = evt.output_tokens
    
    if text_buffer:
        assistant_content.insert(0, LyreContentBlock(type="text", text=text_buffer))
    
    return Turn(
        assistant_message=LyreMessage(role="assistant", content=assistant_content),
        tool_calls=[
            ToolCall(id=b.tool_use_id, name=b.tool_name, input=b.tool_input)
            for b in assistant_content if b.type == "tool_use"
        ],
        stop_reason=stop_reason,
        usage_input=usage_input,
        usage_output=usage_output,
    )
```

### 3.3 `_wait_for_blocker` 内部

```python
async def _wait_for_blocker(self) -> MailboxMessage:
    """轮询 mailbox 直到出现新 blocker（仅 cancel-eligible 的）。
    cancel-eligible：urgency=blocker 且 recipient 是本 agent 或 leader / owner（看 persona 角色）"""
    last_seen_id = self._last_seen_msg_id
    while True:
        new_msgs = await self.mailbox.read_new_blockers(since=last_seen_id)
        if new_msgs:
            return new_msgs[0]              # 多条 blocker 取第一条；其余等下次 turn 处理
        await asyncio.sleep(1.0)            # 1s 轮询
```

### 3.4 `_dispatch_tool` 内部

```python
async def _dispatch_tool(self, tc: ToolCall) -> ToolResult:
    """分发工具调用：Lyre 工具走 MCP server / Unix socket；shell 工具直接 subprocess.exec。"""
    if tc.name in self._lyre_tool_names:
        # Lyre 工具：走 MCP server
        return await self._mcp_client.call_tool(tc.name, tc.input)
    elif tc.name == "shell":
        # 通用 shell 工具：直接执行
        return await self._shell_exec(tc.input)
    else:
        return ToolResult(content=f"Unknown tool: {tc.name}", is_error=True)
```

### 3.5 Mid-loop 中断范围（MVP 决议）

**做**：

- Cancel 正在 stream 的 LLM 调用（asyncio cancel + Anthropic SDK 原生支持 stream 关闭）
- 注入中断消息到下轮 user message

**不做**：

- Kill agent 正在跑的 shell subprocess（如长跑 pytest）；等它返回再 ack blocker
- Mid-stream partial parsing（Anthropic SDK 抛 CancelledError 即可，半成 block 抛弃）

### 3.6 Loop 真实退出条件（重要：跟 stop_reason 解耦）

> ✅ **Current (2026-06-10)**: §3.6–§3.8 describe the shipped loop behavior — trust these sections.

模型 emit 的 `stop_reason` 只是 metadata，**不是控制信号**——尤其 DeepSeek-V4
几乎每个 turn 都返回 `stop_reason="end_turn"` 哪怕同时 emit 了 tool_use blocks。
loop 的退出条件按 Anthropic 原生 agentic loop 语义：

- **本轮 emit 了 tool_use** → 执行 tools，把 tool_results append 到 messages，**继续下一轮**（不看 stop_reason）
- **本轮无 tool_use**（纯文字 / 空响应）→ 退出
- `max_turns` 兜底（默认 24）

历史 bug：旧 loop 在 `stop_reason="end_turn"` 时（即使有 tool_use）直接 break，
导致每次 `mailbox_send` 之后模型再没机会读 tool_result + 继续工作——所有
"ack 完就消失"的 silent failure 都根因于此。修复后 loop 才真正会做"研究 →
回复 → 继续 → 完成"的多步工作流。

### 3.7 Auto-compaction（mid-wakeup）

当某个 turn 的 `input_tokens >= compact_threshold × context_window`（默认 0.7），
loop 自动调用 `runtime/compact.py:compact_messages()` 重写 `messages` 列表：

- 保留：`[initial user msg, 时序保留的 mail in/out synthetic msgs, work summary, 最后 K turn 对]`
- `mailbox_get_message` tool_result → synthetic user msg（owner / peer 原话不丢）
- `mailbox_send` tool_use → synthetic assistant msg（agent 自己说过什么不丢）
- `mailbox_read` listing / `list_*` / `query_task_status` 等 idempotent 工具 → 整体丢
- `shell_exec` / `python_exec` / `dispatch_task` 等 → 调一次同 model 跑 compact prompt 摘要

Thrashing 兜底：单 wakeup compact 超过 3 次仍撑不下 → 强制 `silent_close`，
runtime 自动给 askers 发兜底邮件。每个 wakeup 的 `context_peak_tokens` +
`compaction_count` 落到 `wakeups` 表（migration 0006）+ dashboard 显示。

阈值通过 `LyreConfig.compact_threshold`（env: `LYRE_COMPACT_THRESHOLD`）调。

### 3.8 Silent-close 兜底

如果 wakeup 在 `_MAX_SILENT_TURN_NUDGES`（2）次硬提醒后仍只调了 info-gathering
工具、没调任何 user-facing 工具（`mailbox_send` / `dispatch_task` / …），
loop 会：

1. 收集本 wakeup 期间被 `mailbox_read` 自动标 read 的所有 mail 的 sender
2. 给每个 sender enqueue 一封 outbox 的 `silent-close` 兜底邮件，body 包含
   工具调用摘要 + 最后 assistant text snippet + wakeup_id（给 operator 排错）
3. wakeup 的 `end_status` 设为 `silent_close`，dashboard 用 alert 红色显示

---

## 4. Lyre MCP server（工具暴露层）

> **实现修正 (2026-06-10)**: **never built.** `src/lyre/mcp_server/` is an empty stub (its docstring still cites this section); there is no Unix socket, no MCP client, no forked agent subprocess. Tools dispatch **in-process**: `ToolRegistry` (`runtime/tools/builtin.py`) + `agent_loop._dispatch_tool`, with `allowed_lyre_tools` enforced at spec-build and dispatch. About half of §4.2's routed tools were never implemented (`load_skill` / `propose_skill` / `approve_*` / `mark_pr_reviewed` / `query_local_hot_summary` / `request_review`). A gateway-like seam may return via the parked `PLUGINS.md` spec.

### 4.1 为什么用 MCP

> Model Context Protocol：Anthropic 提出、跨厂商支持的 LLM 工具协议。

理由：

- **MCP 是标准**：Claude Code / Claude Desktop / Cursor / Continue 等都支持
- **一份工具实现两路复用**：MVP 内部 AgentLoop 通过 MCP client 调（Adapter Anthropic-shape 模式）；未来 Claude Code 当 worker 后端时也通过 MCP 连同一个 server
- **JSON-RPC + Unix socket** 跟 A 簇 [`AGENT_CONTRACT.md §4.4`](./AGENT_CONTRACT.md#44-lyre-工具走-gateway) 选定的 gateway 协议天然对齐

### 4.2 MCP server 架构

```
Lyre 主进程
├── Scheduler
├── MCP server (Unix socket: /tmp/lyre/gateway.sock)
│   ├── tools/list       → 返回当前 task 的 ToolManifest.lyre_tools
│   └── tools/call       → 路由到 Lyre 内部实现
│       ├── mailbox_send / mailbox_read / mailbox_get_message / mark_read → MailboxRepository
│       ├── report_progress      → LocalHotRepository.put
│       ├── report_side_effect   → SideEffectRecorder（写 wakeup checkpoint + 派生通知）
│       ├── load_skill / list_skills → SkillRepository.get_by_name / list
│       ├── propose_skill        → SkillRepository.propose
│       ├── approve_skill        → SkillRepository.approve
│       ├── dispatch_task        → TaskRepository.create（含 parent_task_id）
│       ├── query_task_status    → TaskRepository.get
│       ├── propose_persona      → PersonaRepository.propose
│       ├── approve_persona      → PersonaRepository.approve
│       ├── mark_pr_reviewed     → SideEffectRecorder.record (mailbox + outbox)
│       ├── query_local_hot_summary → LocalHotRepository.summary_by_persona
│       └── request_review       → mailbox 派生到 leader（"请评审"）
│
└── Agent subprocess (forked)
    ├── AgentLoop (asyncio)
    │   └── _dispatch_tool → MCP client (Unix socket)
    └── shell exec (subprocess.create_subprocess_exec)
```

### 4.3 实现选型

- 用官方 [Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk)（`mcp` package）
- Server 监听 Unix socket（path = `/tmp/lyre/gateway.sock`）
- 客户端在 agent subprocess 里启动 MCP client，连同 socket

### 4.4 Per-agent context 注入

不同 agent 唤醒可调用的 Lyre 工具不同（由 persona `allowed_lyre_tools` 决定）。MCP server 在 `tools/list` 时按 caller 身份返回该 agent 允许的子集。Caller 身份由 socket 连接时的 task_id / wakeup_id 传入（启动 agent subprocess 时通过 env var 注入）。

---

## 5. Context 装配

> 文件系统优先：User / Skills / Memory 全部从 `~/.lyre/` 下的 markdown 读取，没有向量检索。

### 5.1 装配顺序

```
[Lyre 主进程在 fork agent subprocess 之前装配 AgentInput]

system prompt（拼接，作为 system 参数传给 LLM）:
├── identity preamble                      (~500 token，agent_id / 协议 / mail rules)
├── persona.role_description               (~300 token)
├── persona.system_prompt                  (~1.5k token)
├── ~/.lyre/user.md 整文件注入              (~500 token；缺省时跳过)
├── tier policy summary                    (~200 token)
├── hosting-specific notes                 (~300 token；如"该 repo 在 GitHub, gh pr create...")
├── memory index（facts/* frontmatters）    (~500 token，扫 ~/.lyre/memory/facts/)
├── skills XML（approved skills frontmatters）(~700 token，progressive disclosure)
└── lyre_tools manifest                    (通过 stream_turn 的 tools 参数传，不占 message token)
                                            small total: ~4-5k token

first user message:
├── task.goal                              (~200 token)
├── task.acceptance                        (~100 token)
├── checkpoint summary（续做时）            (~500 token)
└── local_hot 当前状态摘要                  (~300 token)
                                            small total: ~1.5k token

Total initial context: ~5.5-6.5k token. Sonnet 4.6 200k context 内绰绰有余。
```

### 5.2 装配规则

- **User identity**（`~/.lyre/user.md`）：整文件 strip 后注入，无 parser。Owner 改完下次 wakeup 立即生效。
- **Persona system_prompt** + **role_description**：来自 DB `personas` 表（由 `~/.lyre/personas/*.md` 或 shipped `src/lyre/personas/*.md` upsert）。
- **Memory index**：扫 `~/.lyre/memory/facts/` 所有 `.md`，读 frontmatter 拼成索引行。Agent 用 `read_memory` / `shell_exec` 按需加载 body。
- **Skills**：扫 `~/.lyre/skills/approved/`，按 persona / scope 过滤；progressive disclosure（只显示 name + description）。
- **Hosting-specific notes**：由 leader 在派任务时注入 `tasks.metadata.hosting_notes`，原文转发。

### 5.3 续做时的 checkpoint summary

不直接 dump raw checkpoint JSON。由调度器 / leader 生成自然语言摘要：

```
你正在续做任务 task-{id}。你已完成的步骤：
- 已读 src/auth.py 与 tests/test_auth.py（步骤标识 read_code）
- 已应用补丁 v1，commit 到分支 lyre/task-X/fix（步骤标识 patch_applied）
- 测试结果：3 个 pass，1 个 fail（步骤标识 tests_run，记录在 local_hot.scratch_pointers.test_report）

下一步：分析失败测试并决定是改代码还是改测试。
```

### 5.4 Token 经济（MVP 起步策略）

- 初始 context ~6k，每轮 LLM response ~1k，每轮 tool result ~500 token
- 跑 50 轮上限：~6k + 50 × 1.5k = ~80k token，仍在 200k context 内
- **MVP 不做 self-summarization**——超 100k 时再考虑

---

## 5.5. Sequential agents, event-driven all the way down

> ✅ **Current (2026-06-10)**: this section describes the shipped behavior — trust it.

> **English TL;DR**: An agent is a *sequential actor*. Two pending
> tasks for the same agent id run one after another, never
> concurrently. Parallelism within a persona uses multiple agent
> instances (`analyst/auth-tokens`, `analyst/auth-session`), not
> multiple wakeups of one agent. Composers (analyst, reviewer) fan
> out via `dispatch_task` + `create_agent` and synchronise via mail
> + `scheduled_mail`-to-self timeouts — there is no blocking
> "wait for children" primitive.

### 5.5.1 为什么 agent 必须串行

agent 跨 wakeup 的持久状态（scratchpad、notes 末尾的 `## Auto-summary log`、
agent 私有 mail）住在共享文件系统。subprocess 模式下两个 wakeup 同时跑同一
agent_id ≈ 两个进程对同一份文件 read-modify-write —— scratchpad 丢更新、
auto-summary log 内容交错。owner 心智里 `analyst-1` 也是一个 actor，不是
capability pool；并行的语义不该被同一 id 偷偷承担。

### 5.5.2 实现

scheduler `_tick` Phase 3 claim pending task 前查
`wakeups.has_active_for_agent(agent_id)`：

```python
if await self.repos.wakeups.has_active_for_agent(agent_id):
    continue  # this agent is in a wakeup; pending task stays pending
```

同一 tick 内也维护一个 `claimed_in_this_tick: set[str]` 防止同 tick 多 claim。

并行靠**实例**：

- `analyst-1`（默认 seed instance）
- `analyst/auth-tokens`、`analyst/auth-session`（按 sub-topic spawn 的并行实例）
- `analyst/research-X` 跑完留下来下次复用（pool semantics）

### 5.5.3 No blocking primitive — event-driven 是唯一同步路径

runtime **删除**了 `await_subagents` 工具和支持机制（`TaskRepository.
find_parents_ready_to_wake` / `wake_parent` / scheduler 的 wake-parent
phase）。理由：

- dispatcher 是 owner-facing singleton，blocking 直接 DoS owner
- 即便不是 singleton（analyst），blocking task 进 `needs_input` 也会让
  Phase 0 auto-wake-on-mail 跳过它——比如有更紧急的活突然进来无法被打断
- 保留 await 等于保留一个「在某些场景看似方便、其他场景灾难性」的反模式
  诱惑，不如整套删干净

合成型角色（analyst 拆并行子调研、reviewer 多 PR 并行评）的正确范式：

1. `create_agent(persona="analyst", name="<sub-topic>") × N`
2. `dispatch_task` 派每个子任务
3. `mailbox_send(to=<self>, deliver_in="30m", ...)` 软超时
4. `update_scratchpad(append="dispatched N sub-tasks, waiting on: ...")`
5. 停止调 tool，wakeup 关闭

子任务回信 → auto-wake → 读 scratchpad 看还在等谁 → `query_task_status` 看
进度 → 增量消化。所有子任务都凑齐了再综合写最终输出。

### 5.5.4 取舍

| | 顺序 actor（当前） | 并行 actor（previous） |
|---|---|---|
| scratchpad / notes 并发 | 不可能 | race，丢更新 |
| owner 心智 | 一个 id = 一个 actor，直觉 | 一个 id = 多个跑着的实例，confusing |
| 并行手段 | spawn 多 instance | 同 instance 多 wakeup |
| pending task 在 dashboard 上 | "排队"显式可见 | 隐式并发，调试难 |
| 吞吐 | 同 agent 顺序 | 同 agent 并行（但 race） |

吞吐损失主要发生在「dispatcher 一次性派两个 task 给同一 agent」——但这本来
就是反模式（dispatcher 应该派给不同 instance 才能并行）。新机制把这个反模式
从隐式（数据 race）变成显式（task 排队、dashboard 上看得到），是改进。

---

## 6. Failure 与 retry

| 失败 | 处理 |
|---|---|
| LLM API timeout / 5xx | 指数退避重试 3 次（1s / 2s / 4s）；仍失败 → AgentOutput.status=failed，失败计数 +1 |
| 429 rate limit | 按响应头的 `retry-after` 等待；总共重试 3 次 |
| Tool call schema 错（LLM 没按 schema 出 input）| 返回 error tool_result 给 LLM，让它重试；同一工具同一调用累计 3 次错 → 提示 LLM 换策略；仍失败 → failed |
| Lyre 工具调用失败（gateway / DB 错）| 返回 error tool_result；可重试 |
| Shell 命令失败 | 返回 stderr + exit code 给 agent；agent 自决 |
| Loop 超 max_iterations=50 | status=needs_continuation；保 checkpoint；调度器决定下一轮唤醒 |
| Mid-loop 中断（被 blocker cancel） | 注入中断 message 到下轮；不计 failure |
| Stream 中途断（网络）| 当 retry 处理 |

---

## 7. Metering 与 transcript 写入

### 7.1 Metering

每轮 LLM 调用累加 `Usage` event 的 `input_tokens` / `output_tokens` 到 `MeteringResult`：

```python
@dataclass
class MeteringResult:
    token_input: int
    token_output: int
    wall_clock_ms: int
    tool_call_count: int                    # 仅计 Lyre 工具调用
    provider: str                           # "anthropic"
    model: str                              # "claude-sonnet-4-6"
```

写入 `wakeups` 表的对应列（[`PERSISTENCE_SCHEMA.md §3.1`](./PERSISTENCE_SCHEMA.md)）。

### 7.2 Transcript 写入（streaming）

`TranscriptWriter` 在 stream 期间持续写 cold archive：

```python
class TranscriptWriter:
    """写 wakeups/{wakeup_id}/transcript.jsonl"""
    
    def __init__(self, wakeup_id: str, object_store: ObjectStore):
        self.path = f"wakeups/{wakeup_id}/transcript.jsonl"
        self.stream = object_store.open_append(self.path)
    
    def write_delta(self, text: str):
        # 文本块的增量，写 jsonl 行
        self._write_json({"type": "content_delta", "text": text, "ts": now()})
    
    def write_tool_use(self, id, name, input):
        self._write_json({"type": "tool_use", "id": id, "name": name, "input": input, "ts": now()})
    
    def write_tool_result(self, id, result, is_error):
        self._write_json({"type": "tool_result", "id": id, "result": result, "is_error": is_error, "ts": now()})
    
    def note(self, text: str):
        self._write_json({"type": "note", "text": text, "ts": now()})
    
    def close(self):
        self.stream.close()
```

唤醒结束时 `close()`；`wakeups.transcript_uri` 字段记 path。

### 7.3 Tool call 日志

可选：`wakeups/{wakeup_id}/tool_calls.jsonl` 记每次工具调用的完整 input / output（便于事后审）。MVP 暂时合并进 transcript.jsonl，未来流量大再拆。

---

## 8. 未来扩展（OpenAIAdapter / Claude Code via MCP / 其它）

### 8.1 OpenAIAdapter（不在 MVP）

接口照 LLMAdapter Protocol 实现一份。要点：

- 用 `from openai import AsyncOpenAI`；同样支持 `base_url` 参数
- Tool 转换：`{"type":"function", "function":{"name":..., "description":..., "parameters":...}}`
- Tool call 响应解析：`message.tool_calls`，`arguments` 是 JSON string 需 parse
- Stream event：OpenAI 的 stream 用 `chunk.choices[0].delta`；映射到 `ContentDelta` / `ToolUseStart` / `ToolUseComplete` / `Usage`
- 约 200 行代码

### 8.2 Claude Code via MCP（不在 MVP，但 Lyre MCP server 同步搭好）

启动 Claude Code 作为 agent subprocess 后端：

```python
class ClaudeCodeAdapter:
    """不用 LLMAdapter Protocol——Claude Code 自带 agentic loop。
    我们只负责：启动 Claude Code、配置它连 Lyre MCP server、收集结果。"""
    
    async def run_wakeup(self, input: AgentInput) -> AgentOutput:
        # 1. 写 .mcp.json 指向 Lyre socket
        # 2. 用 Claude Code SDK 或 CLI 启动
        # 3. 收集结果
```

### 8.3 多 provider 路由

未来 leader-persona 根据 persona `model_routing` 决定唤醒哪个 adapter：

```python
def make_adapter(persona, env) -> LLMAdapter:
    routing = persona.model_routing
    primary = routing["primary"]
    if primary["provider"] == "anthropic":
        return AnthropicAdapter(
            api_key=env[primary["api_key_env"]],
            base_url=primary.get("base_url"),
        )
    if primary["provider"] == "openai":
        return OpenAIAdapter(...)
    raise ValueError(f"Unknown provider: {primary['provider']}")
```

---

## 9. 已识别但待解决的问题

1. **MCP server 的认证 / 多 caller 区分**：Unix socket 文件权限 = 认证（同 owner 用户）；多 agent 同时连同一 socket 时怎么区分 caller？通过连接初始化时传 `task_id + wakeup_id`。具体协议 message 待定
2. **Mailbox watcher 升级到推送**：MVP 1s 轮询；升级到 Lyre gateway 主动推送（agent 在连接时声明"我关心这些 recipient 的 blocker"）。v0.2 / v0.3 候选
3. **Stream 中断的清理**：cancel 后 SDK 是否完全清理 HTTP 连接？需要测试确认 Anthropic SDK 的行为
4. **重新计算 checkpoint summary 的开销**：续做时由谁生成（leader-persona agent 还是规则脚本）？v0.1 倾向规则脚本起步，未来升级到 LLM 生成
5. **Tool result 大小限制**：单个工具返回过大（如 `read_file` 读 1MB 文件）会撑爆 context。MVP 加截断 + 提示 "truncated, use targeted query"
6. **shell exec 的输出捕获**：stdout/stderr 大量输出时如何流式回给 LLM 而不超 context？MVP 截断到 4k token + 把完整 output 写 local_hot blob
7. **Agent loop 是否可被 Lyre 主进程直接取消（kill subprocess）**：与 mid-loop 中断（cancel LLM）不同；前者更彻底（拔线），适用 owner "STOP ALL"。v0.1 留接口，由 Lyre 主进程的 ProcessManager 实现
8. **Adapter 切换的运行时热切**：长任务途中切 provider（如主 provider 限流后 fallback）。MVP 简化：fallback 触发新唤醒；不在同一唤醒内切

---

