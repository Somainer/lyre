# Lyre — 失败韧性(dispatcher 监督 + API 调用韧性)

> **文档定位**:来自一次 **OBSERVED** 事故——dashboard 上 `dispatcher · wakeup failed · persona default · ctx —`,且**没有任何人 supervise 到**。深挖发现这是两层问题:(1) **监督层**——bootstrap 单例(dispatcher 等,`parent_agent_id=NULL`)的 wakeup 失败有三个洞让"该通知 owner"不触发;(2) **预防层**——LLM 调用的重试/failover 很薄,尤其**流中失败当场致命**,把一次瞬时 API 抖动变成 wakeup 死亡。本轮:**预防(R1+R2)+ 安全网(A+C)**。
>
> **English one-liner**: An observed "dispatcher wakeup failed, nobody noticed" bug. Two layers: a SUPERVISION gap (a bootstrap singleton's root-task failure doesn't reach the owner — the `auto_dispatched` suppression eats failures, and transient setup failures retry-loop unbounded with no escalation) and a PREVENTION gap (LLM-call resilience is thin: per-call retry is SDK-default-only and untunable, and a mid-stream failure is fatal with zero fallback). Fix = safety-net (A: escalate auto-dispatched failures; C: bound+escalate bootstrap-singleton recovery) + prevention (R1: explicit tunable SDK retry; R2: mid-stream fallback, gated on an adversarial review).
>
> **相关**:`scheduler.py`(`_emit_task_terminated_mail` / `_resolve_terminated_task_supervisor` / Phase-2 lease recovery / `_ephemeral_recovery_exceeded`);`AGENT_RUNTIME.md` + `AGENT_CONTRACT.md`(thinking-block↔tool-use 绑定约束,R2 触及);`adapter/anthropic.py` + `adapter/openai.py`(SDK client 构造)。
>
> **owner 对齐**:2026-06-09。范围 **A + C + R1 + R2**(四个一起),**R3 负载均衡缓**(未观测、failover 已给韧性),**R4** 仅查现状(是否有 persona 只解析出 1 个候选 = 零 failover)。R2 **动手前先过一轮对抗式审视**(thinking-block 绑定 subtle)。

---

## 1. 现状与三+二个洞(均 file:line 实证)

### 监督层(已有意修过一半)

`_emit_task_terminated_mail`([scheduler.py:1014](../../src/lyre/scheduler/scheduler.py))的 docstring(:1040-1043)**已经写明**意图:top-level 任务**只在 FAILURE 时**通知 owner——"a silent failure is exactly the 'sudden failed 没人知道' gap we close"。`_resolve_terminated_task_supervisor`(:965)也已有 **owner 兜底**(无 parent → 发 owner)。所以"根任务失败→通知 owner"这条路**存在**。但对 dispatcher 不触发,因为:

- **洞 A**:gate(:1048)`if meta.get("fan_in_group") is not None or meta.get("auto_dispatched"): return` —— 对 `auto_dispatched` **无条件 return**,**连失败一起吞**。dispatcher 的 wakeup 几乎都是 Phase-0 因 owner 来信而起的"check inbox"任务,带 `auto_dispatched=True`(:569)。→ 即使失败走到 emit,也被吞。
- **洞 C-i(确定性 setup 失败静默)**:setup 阶段 `persona is None`(:1477)、git-provision 失败(:1545)是 **mark-failed-then-return**,**不 emit** → 静默。(git-provision 仅 worker 有 git_context;persona-None 对单例是严重配置错却静默。)
- **洞 C-ii(瞬时 setup 失败无界重试)**:worktree/mailbox(:1564)、blocker-watcher(:1577)的失败是 **re-raise**,冒泡到 tick 边界被 `run()` 的 `except`(:302)记一条 `scheduler_tick_error` 就完了;lease/wakeup 故意留悬(注释 :1508-1510),下个 tick `find_expired_leases` 当过期租约捞回重试——对**瞬时**失败是对的,但 `_ephemeral_recovery_exceeded`(:365)的重试上限**只给 ephemeral**,**bootstrap 单例没有** → 确定性失败**静默无限重试**。**你观测到的 `persona default · ctx —` 正是这一桶**(wakeup row 已建[过了 :1484]但 loop 没跑[ctx 空])。

> **runtime 不会因此崩**:setup 异常冒泡到 `run()` 的 `except`(:302),记日志后**继续下一 tick**。问题纯粹是**静默 + 无界重试**,不是宕机。

### 预防层(LLM 调用韧性很薄)

- **跨候选 failover(per-turn)**:`_run_one_turn_with_fallback`([agent_loop.py:1070-1238](../../src/lyre/runtime/agent_loop.py))逐个试 router 排好的候选;候选**出首 token 前**失败 → `mark_failure`+`continue` 下一个。**全部候选都失败** → `raise AllCandidatesFailedError`(:1238)→ wakeup failed。
- **健康熔断(cross-wakeup)**:`health_tracker` 60s 内失败 3 次熔断 180s。
- **洞 R1**:SDK 自带重试(Anthropic/OpenAI 默认重试 408/409/429/500/529 约 2 次)**有效但 Lyre 没传 `max_retries`**([anthropic.py:44-64](../../src/lyre/adapter/anthropic.py)、[openai.py:70-87](../../src/lyre/adapter/openai.py))→ **SDK 默认、不可调、不可见**。持续 529 超过那 2 次 → 候选当场烧掉。
- **洞 R2(最尖锐)**:**流中失败当场致命**。一旦出了首个事件(`yielded_any=True`),之后流断(连接掉 / 生成中途 529)→ agent_loop.py **直接 `raise`(~:1220),不 fallback、不重试** → wakeup failed。有意为之、有测试钉着(`test_agent_loop_fallback.py::test_midstream_error_propagates_and_no_retry`)。
- **洞 R3(负载均衡)**:严格确定性排序 failover(永远先 candidate[0]),**无 LB**。→ **本轮缓**(个人 runtime failover 已给韧性,LB 是吞吐/限流问题,未观测)。

---

## 2. A:auto_dispatched 失败也通知 owner

把 gate(:1048)从"无条件吞 auto_dispatched"改成**只吞非失败终态**(对齐 :1050 对根任务的处理):

```
if meta.get("fan_in_group") is not None: return          # barrier 自管这些(不变)
if meta.get("auto_dispatched") and task_status != "failed": return  # 例行收件箱「完成」是噪音;「失败」不是
if task.parent_task_id is None and task_status != "failed": return   # owner 只需根任务的失败(不变)
```

→ auto_dispatched 的 **failed** 现在穿过三道 gate → `_resolve_terminated_task_supervisor` → owner。**完成/取消仍静默**(不烦 owner)。约 3 行。

## 3. C:bootstrap 单例失败必达 owner、不静默循环

目标:**单例的 wakeup 失败,确定性的立刻 escalate,瞬时的有界重试后 escalate**——两类都不再静默。

- **C-i 确定性 setup 失败立刻 escalate**:`persona is None`(:1477)等"重试也没用"的 mark-failed 路径,在 mark failed 的同时调 `_emit_task_terminated_mail`(走 A 修好的 owner 路径)。不重试(终态),立刻响。
- **C-ii 瞬时 setup 失败有界 + 超界 escalate**:把 `_ephemeral_recovery_exceeded`(:365)那套"重试预算"**延伸到 bootstrap 单例**(单例 id 已有 `list_bootstrap_singleton_ids`,:395)。Phase-2 lease 恢复同一 task 累计 N 次仍未成功 → 不再重跑,落 `failed` + emit owner。瞬时失败(第 2 次就成)**不** escalate;确定性失败 N 次后 escalate。**这正命中观测到的那次**。
  - 计数面:复用既有恢复计数机制(`_ephemeral_recovery_exceeded` 读的那处),把"仅 ephemeral"的判定放宽到"ephemeral **或** bootstrap 单例"。预算可配(沿用现有 restart-intensity 旋钮或加一个 `singleton_recovery_max`,默认比如 3)。

> **谁 supervise dispatcher?** 答案是 **owner(你)**——`_resolve_terminated_task_supervisor` 的 owner 兜底就是这个语义。本轮不新增"监督 dispatcher 的 agent"(那只把问题往上推一层);只把已存在的 owner-escalation 对单例补好。

## 4. R1:显式、可调的 SDK 重试

给 `AsyncAnthropic`/`AsyncOpenAI` 构造传 `max_retries`(来自 config,如 `LYRE_LLM_MAX_RETRIES`,env > `[runtime]` > 默认 **2**[与当前 SDK 默认一致,不改行为]),并 log 出来。把"隐式 SDK 重试"变成**显式、可调、可见**。覆盖**建连/出首 token 前**的瞬时错误(429/529/500/timeout,SDK 带 backoff)。纯 adapter 层,守铁律一。

## 5. R2:流中失败 → 有界 next-candidate failover —— **对抗式审视已裁决:SAFE**

**裁决(workflow `wf_bdc9471f-e0c`,prosecution / defense / safety-audit 三腿全部认同安全)**。代码实证:(a) 工具在 turn 返回**之后**才 dispatch(agent_loop.py:659/665),流中 `raise` 在 :1224 return 之前退出 → **无工具跑、无 side effect**;(b) assistant 消息只在 caller :655/695 的**正常返回路径**追加,流中异常**不留 partial** 进 messages;(c) 流中只写 transcript(冷、append-only、不回读 → law4)+ 读 blocker_watcher.signal + `health.mark_failure`(熔断计数)——**都不污染** messages/DB;(d) thinking-block↔tool-use 绑定约束是关于**已持久化的 NEXT API call**,丢弃的 turn 从未进 messages → **不适用**。原"流中致命"是 MVP 取巧("no partial-output retry in MVP"),**非正确性守卫**。

**定稿设计**:把 mid-stream 分支(`yielded_any=True`,~agent_loop.py:1220)的无条件 `raise` 改为**有界 next-candidate failover**:
1. 每个 turn 一个 `midstream_attempts` 计数,cap `max_midstream_retries`(默认 **1**,可配);超界 → `raise`(保持今天的致命语义,经 A/C escalate 给 owner)。
2. **next-candidate 优先,不做 same-candidate 重试**——R1 的 SDK `max_retries` 已覆盖同端点瞬时重试(在 `stream_turn` raise 之前);再叠同候选重试只会捶已过载端点。直接 `continue`(partial 随候选 locals 在循环顶重置而自然丢弃,:1097-1104;messages 不动)。
3. 丢弃/重试**前** `transcript.note("midstream_failover: …")` 打标——transcript 被 dashboard/`tail` 实时读,无标会让重跑 turn 看着像重复输出(defense 的关键 cosmetic 顾虑)。
4. `fallback_event` reason=`midstream_error`(区别 pre_stream);保留 `health.mark_failure`(:1200)+ `is_available`(:1071)让熔断照常,跨 turn 由熔断兜底。
5. **不碰 interrupt 路径**:`interrupted_mid_stream` 的 clean-break 在 except 之前 return,天然不受影响。
6. 全候选 + 重试耗尽 → 仍 `raise AllCandidatesFailedError`(:1238 同款),wakeup 干净 failed、task 重派,kill-test 不破。

**实现前必须钉的前提**:安全证明依赖"所有 adapter 的 `stream_turn` 流中纯产事件、零 durable side effect"。anthropic 已确认(纯 read→yield);**grep 其余 adapter(openai/deepseek/openrouter/vllm)确认无流中写**,并加测试钉死"流中 partial tool_use **从不** dispatch"(防未来 adapter 破坏)。

**缓做(非本轮)**:错误分类(对非瞬时 400/422 不 failover、保持致命)——这是 **pre-stream 路径今天也有的**前提(pre-stream 对任何异常都 failover),且分类要懂 provider 错误类型、应落 adapter 层(铁律一)。本轮 R2 与 pre-stream 行为对齐即可;分类作为对**两条路径**的未来精炼。

**测试**:替换 `test_midstream_error_propagates_and_no_retry` 为:(a) 流中失败 → next-candidate → wakeup 完成 + `fallback_event` reason=midstream_error + messages 无 partial;(b) cap 耗尽 → raise + messages 不变;(c) **partial tool_use 从不 dispatch**(spy `_dispatch_tool`)钉死"工具仅 post-turn"。

---

## 6. 验收(离线 mock)

- **A**:auto_dispatched 根任务终态 `failed` → owner 收到 `task_terminated` 高优邮件;同任务 `completed` → **不**发。fan_in 腿仍静默。
- **C-i**:单例 task persona 缺失 → 标 failed **且** owner 收到 escalation(对照:修前静默)。
- **C-ii**:单例 task setup 连续 re-raise:第 2 次成功 → **不** escalate(瞬时自愈);累计超 N 次 → 落 failed + owner escalation,**且不再重跑**。
- **R1**:config 解析(env > toml > 默认 2);adapter client 以该 `max_retries` 构造(注入假 client 断言)。
- **R2**:**裁决通过则**——流中失败后 fall back 到健康候选、wakeup 成功(替换旧的 `propagates_and_no_retry` 测试,改名/改义);带 guard 条件断言无重复 side effect。**裁决否则**——保留旧测试,文档记否决理由。

## 7. 五铁律 / kill-test 辩护

- **铁律一(provider 中立)**:R1/R2 只动 `adapter/` + `agent_loop`(`max_retries` 是通用 client 参数;R2 是 loop 内候选迭代)。A/C 纯 scheduler。零 adapter 接口变更。
- **铁律三(拔线)**:A/C 的状态改写走**既有围栏终态 commit**(`update_status(holder_wakeup_id=)` + `still_holder` gate);C-ii 的恢复计数是**持久行**(task metadata / 既有恢复计数),kill 后下个 tick 重评估,上限仍成立;R2 的重试在**单 wakeup 内**(内存态),wakeup 结束即弃,kill 中途 → 同今天的 lease 恢复。
- **铁律五(mailbox 唯一)**:A/C 的 escalation 走**既有 `task_terminated` mailbox 路径**(barrier/supervisor 同款),无旁路。

## 8. 分 PR / 顺序 / 非目标

| 件 | 内容 | 关键测试 | 大小 |
|---|---|---|---|
| **A** | gate 放行 auto_dispatched 的 failed | failed 发 / completed 不发 / fan_in 仍静默 | S |
| **C** | 单例失败必达 owner:确定性立刻 emit + 瞬时有界重试超界 escalate | 确定性即 escalate / 瞬时自愈不 escalate / 超界 failed+escalate | M |
| **R1** | 显式可调 SDK `max_retries` | config 解析 + client 构造注入断言 | S |
| **R2** | 流中失败 → 有界 next-candidate failover(cap=1、打标、不碰 interrupt) | next-candidate 完成 / cap 耗尽 raise+messages 不变 / partial tool_use 不 dispatch | M |

**顺序**:A✅ → R1(都 S、独立)先落;C(M)随后;R2(审已过)落。本轮一个 PR(主题一致),内部可按 commit 分。

**非目标(本轮不做)**:
- **R3 负载均衡** —— failover 已给韧性,LB 是 scale 的吞吐/限流问题,未观测。
- **R4** 只查现状:是否有 persona 的 `model_preference` 只解析出 1 个可达候选(= 零 failover);若有,是**配置**修(registry/persona),不在本轮代码内。
- 不新增"监督 dispatcher 的 agent"(owner 即其 supervisor)。
