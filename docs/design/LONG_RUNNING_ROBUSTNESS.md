# Lyre — 长跑健壮性:压缩硬化 + 记忆卫生

> **文档定位**:针对"跑数月到数年的多 agent 组织"这一目标,补齐两块**与负载无关**的健壮性短板——(1) in-wakeup 上下文压缩(`runtime/compact.py`)在**重复压缩**下会静默销毁逐字保留的 mail;(2) per-agent notes(`runtime/wakeup_summary.py`)**只追加、永不修剪**,长跑必然无界膨胀并反噬 context。两者都不改任何 agent 契约、不碰五铁律内核。
>
> **English one-liner**: Two workload-agnostic durability fixes for a runtime meant to run for months/years — make in-wakeup compaction idempotent so a *second* compaction can't destroy the verbatim mail the five laws require kept, and rotate the unbounded per-agent notes log down into the cold-archive tier so reading one's own notes doesn't blow the context window.
>
> **相关**:[`FOUNDATION.md`](./FOUNDATION.md) 铁律三(kill-test)、铁律四(三档持久层)、铁律五(mailbox 唯一);[`AGENT_RUNTIME.md`](./AGENT_RUNTIME.md) §6 压缩;[`AGENT_THREADS.md`](./AGENT_THREADS.md) push-context(本设计与之互补:T1 管"注入什么进 context",本文管"context 撑爆时怎么收、记忆怎么不膨胀")。
>
> **状态**:RB-1、RB-3 已落地;RB-2 设计定稿待实现(见 §6)。

---

## 1. 背景:负载形态决定健壮性重点

Lyre 的目标是一个长期运转的多 agent 组织,而非单一负载的机器人。owner 的意图、外部信息、各 agent 的产出持续流入,被 dispatcher / analyst / worker / long-runner 消化、归并、回报;负载是**异质的**(调研、归纳、对比、监控、偶尔写代码),不绑定 git,也不绑定 "coding"。

这种形态对运行时健壮性派生两条结构性后果,二者都与"agent 在干什么"无关:

1. **持续消化大量 inbound → 压缩是高频路径**。一个长期吞吐 mail / 工具输出的 agent,单个 wakeup 内多次撞压缩阈值是常态。压缩路径的正确性因此是高频不变量,而非边角情形。
2. **知识随时间单调累积 → 记忆无界增长**。agent 的 notes / 自动摘要是其长期沉淀,且"读自己的 notes"是常规动作。缺少有界化机制时,记忆文件随运行时长无界膨胀。

本文针对的两个缺陷(§2)正落在这两条路径上。

---

## 2. 问题(对照源码)

### RB-1(严重):recompaction 静默销毁 verbatim mail

`compact_messages`(`compact.py`)**第一次**压缩把历史改写成:

```
[初始 user 消息] + [合成 mail-in/out 消息…] + [work-summary 缝] + [最后 K 个 turn-pair]
```

其中"合成 mail"与"summary 缝"都是**纯 text 消息,没有 tool_use 块**。该函数**不是幂等的**:wakeup 内若再次撞阈值(`agent_loop.py:604-635`),会对**已压缩过的列表**再压一次。第二次压缩时,`_extract_synthetic_history` 只对 `tool_use` 块产出合成消息——那些纯文本的已保留 mail 落入 elided 区后**产不出任何东西,被整段丢弃**,被一句 "no substantive tool work" 占位符取代。

`_MAX_COMPACTIONS = 3` 的 thrashing-bail(`agent_loop.py:610`)在第 3 次压缩后 silent-close,只掩盖了现象——但**第 2 次压缩已经销毁了铁律五要求逐字保留的 owner/peer 通讯**。在高频压缩的负载下,这是真实、静默的数据损失。

### RB-2:summary 调用失败完全静默

`_call_for_summary`(`compact.py`)吞掉所有异常返回 `""`,`_make_work_summary_msg` 退化成贴原始 trace。压缩"成功"但内容已降级,而 **wakeup 行 / `lyre wakeups list` 无任何降级信号**——operator 无从知道某次压缩的工作摘要其实是未经 LLM 提炼的原始 trace。

### RB-3:notes 无界增长 → 反噬 context

`_append_to_notes`(`wakeup_summary.py`)每个 wakeup 结束把 `### <ts> · wakeup` 条目往 `## Auto-summary log` 头部插,**从不修剪**。长跑的 agent 这文件累积成千条目。而 `read_memory("facts/agent-<id>-notes.md")` 是常规动作(identity preamble 即指引 agent 读自己的 notes),`memory.py` **不 cap body**——一次 read 把整个无界文件载进 context,反过来更频繁触发 RB-1 那条压缩路径。按铁律四,陈旧条目应下沉到 cold-archive,hot notes 保持有界。

---

## 3. RB-1:压缩幂等(标记 + carry-forward)

核心是让压缩函数能认出**自己上一次的产出**,在再次压缩时把它们原样保留、永不再 elide;只对"上次压缩之后新增的、未标记的 turn"重新 summarize。

- **标记**:`LyreMessage` 加 `compaction_artifact: bool = False`(`adapter/llm_adapter.py`)。压缩产出的合成 mail-in / mail-out / summary 缝都置 `True`。adapter **完全忽略**此字段,永不到达 provider(adapter 只读 `role` + `content`)。零 DDL、向 provider 透明、对所有既有 `LyreMessage` 构造点向后兼容(默认 `False`)。
- **carry-forward**(`compact_messages`):

```python
carried = [m for m in elided if m.compaction_artifact]   # 上次压缩产出 → 原样保留
fresh   = [m for m in elided if not m.compaction_artifact] # 真正的新 turn → 才 summarize
synthetic, work_trace = _extract_synthetic_history(fresh)
new_msgs = carried + synthetic
if work_trace or not carried:        # 有新工作要折叠,或首次压缩(carried 空)才出 seam
    new_msgs.append(await _make_work_summary_msg(...))
return kept_head + new_msgs + kept_tail
```

- **不累积空 seam**:recompaction 若 fresh 区只有 mail(无 trace 类工具工作),`work_trace` 为空且 `carried` 非空 → 跳过新 summary 缝(carried 里的旧缝已代表那段被压历史),否则每次压缩都白增一个空标记。
- **单次压缩路径不变**:首次压缩 `carried` 恒为空,走 `not carried` 分支照常出 seam,行为与改动前一致。

结果:压缩对"逐字保留的 mail"幂等;重压缩真正缩小(只重提炼新工作)而非毁掉历史。`_MAX_COMPACTIONS` 仍作为真·失控(单 turn 输出即撑爆窗口)的 backstop,但不再掩盖数据损失。

---

## 4. RB-2:压缩降级可观测

- `compact_messages` 回传"summary 是否降级"(LLM 调用失败、退化成原始 trace)。最小侵入:返回值由 `list[LyreMessage]` 改为轻量 `CompactionOutcome(messages, summary_degraded: bool)`,或经回调累计。
- `agent_loop` 把降级计入 wakeup 行(类比既有 `compaction_count`),新增 `compaction_summary_degraded` 计数 + 结构化日志事件。
- `lyre wakeups list` 透出该计数(类比既有 `--has-compaction` 过滤),使"压缩质量打折"的 wakeup 可被筛出复盘。

不改 agent 契约,纯 runtime + CLI 观测面。

---

## 5. RB-3:notes 轮转 → cold-archive

- **触发**:`_append_to_notes` 追加后,若 `## Auto-summary log` 区**条目数**超阈值(config `[scheduler] notes_max_entries` / env `LYRE_NOTES_MAX_ENTRIES`,env-beats-toml;0=关,负数/垃圾→0),触发轮转。
- **轮转**:把**最旧**的条目 append-only 下沉到单文件 `object_store/notes_archive/agent-<id>.md`(冷档,只读累积,oldest-first 便于人读),hot notes 留**最近 `notes_max_entries // 2` 条**(轮转即减半,摊销后续触发频率)+ 一行指针(`> _Earlier auto-summaries archived to …_`)。**手写区(`## Auto-summary log` header 之前)绝不动**——那是 agent 长期沉淀的领域知识。entry 边界用**锚定 + 时间戳定形**的正则识别(`^### <ISO8601Z> · wakeup <hex>$`),模型摘要正文里的散装 `### ` 不会被误判为边界;指针行在重解析前剥除,不累积、不被并进尾条目。
- **kill-safe(铁律三)**:固定写序 **先 append 冷档(fsync)→ 再 rewrite hot 文件(write-temp + rename)**。中途 SIGKILL:冷档已落、hot 未截 → 条目同时在两处 → 下次轮转按 `wakeup` 短 id 幂等去重,至少一次、不丢、不重。hot rewrite 用临时文件原子 rename,永不半写。
- **位置**:在 `wakeup_summary.summarize_and_append` 末尾内联(它本就在 wakeup-finalize 写 notes),不新增调度器 phase。`Config.object_store_path` 经调用参数 thread 进去。
- **契合铁律四**:notes(global,单调精炼)的陈旧过程条目下沉到 cold-archive(海量、只读、按指针取)——三档持久层"只存结论密度、过程密度进冷档"的字面执行。

---

## 6. 分 PR 路线

| PR | 内容 | 关键离线测试 | 状态 |
|---|---|---|---|
| **RB-0** | 设计文档(本文) | review anchor | 落地 |
| **RB-1** | recompaction 幂等:`LyreMessage.compaction_artifact` + carry-forward;重压缩绝不丢 verbatim mail,只重提炼新工作 | 标记自身产出 / 二次压缩保 mail / 不累积空 seam;既有压缩测试全部不变 | 落地 |
| **RB-2** | 压缩降级可观测:`compact_messages` 回传 `summary_degraded` → wakeup 行计数 + 结构化日志 + `lyre wakeups list` 透出 | summary 失败→标降级 / 成功→不标 / 多次压缩累加 | 待实现 |
| **RB-3** | notes 轮转 → cold-archive:超阈最旧条目下沉 `object_store/notes_archive/agent-<id>.md`,hot 留最近 `max//2` + 指针;手写区不动;append-cold-先于-rewrite-hot 保 kill-safe;`notes_max_entries` config(env-beats-toml,默认 0=关) | 超阈轮转 / oldest-first 归档 / 手写区保留 / 阈值内不动 / 阈值 0 关闭 / 无 object store 不动 / 重复归档按 wakeup-id 去重 / 多轮不丢不重 / config 三态 | 落地 |

> **依赖序**:三者独立。RB-1 优先级最高——它是会静默丢数据的缺陷。RB-2 / RB-3 可任意顺序。

---

## 7. 五铁律辩护

- **铁律一(provider 中立)**:全部纯 Python + FS;`compaction_artifact` 是 `LyreMessage` 内部字段,adapter 不读、不传 provider。zero `adapter/` 改动。
- **铁律三(拔线)**:RB-1 的标记随 messages 列表活在内存(本就是纯缓存,wakeup 结束即弃,与 kill-test 无关);RB-3 的轮转 append-cold-先行 + 原子 rename,中途 kill 至多重复一条(幂等去重),绝不丢。
- **铁律四(三档持久层)**:RB-3 把 notes(global)的陈旧过程条目下沉 cold-archive,字面执行"结论密度留 global、过程密度进冷档"。
- **铁律五(mailbox 唯一)**:RB-1 的全部意义是**捍卫**铁律五——重压缩不得销毁逐字保留的跨 actor 通讯。无新增通讯通道。

---

## 8. 明确非目标

- **不改 agent 契约 / 工具集 / persona**——本范围纯 runtime 健壮性。
- **不引入向量检索 / facts ranking**——RB-3 只管 per-agent notes 的有界化,不碰 `facts/` 的检索算法(`PERSISTENCE_SCHEMA.md` 单列的开放议题)。
- **不做 cold-archive 的回读进 runtime**——归档只读、按指针人工 / CLI 取用,绝不自动载回 context(铁律四)。
- **不改 `_MAX_COMPACTIONS` 语义**——它仍是失控 backstop;RB-1 只是让它不再掩盖数据损失。
