# Lyre — 主线(thread)上下文连续性 + 有界自驱动

> **文档定位**:解决两个同源问题——(1) 无状态 wakeup 的**上下文健忘**(agent 不会自动重建一条主线的相关 mail / scratchpad);(2) 没有 **runtime 强制的长跑自治**(agent 自限不可靠)。两者共享同一根骨架:一个 **mailbox-native 的 `thread_id`(主线)**。设计原则:**能用 mailbox 原语解决的就用它**(铁律五),**能组合现有 primitive 的就不另起炉灶**。
>
> **English one-liner**: A mailbox-native `thread_id` that the runtime propagates mechanically (reply/dispatch inheritance) becomes the key for two things — auto-loading a thread's context into each otherwise-stateless wakeup, and scoping a budget the scheduler enforces on a "loop" that is just a *budgeted recurring self-mail*. No new tables, no dedicated loop machinery.
>
> **相关**:[`FOUNDATION.md`](./FOUNDATION.md) 铁律五(mailbox 唯一)、铁律三(kill-test);[`AGENT_RUNTIME.md`](./AGENT_RUNTIME.md) wakeup 与 context 组装;[`WORKFLOW_ORCHESTRATION.md`](./WORKFLOW_ORCHESTRATION.md) fan-in barrier / scheduled mail / PR1 park-resume(本设计复用)。
>
> **状态**:**已落地**——T1–T4 全部 shipped in PR #45(commit `d2cf778`,2026-06)。设计经 RCA `019e8d7d` 复盘 + 双 agent 评审收敛;PR 路线见 §7。

---

## 1. 起因:RCA `019e8d7d` 暴露的两个根

复盘见会话记录。除了"dispatcher 把自己当前 wakeup 的 task 误报成在跑的活"(已由 self-wakeup guard + `query_task_status` 增强修掉)外,留下两个更深的结构问题:

1. **上下文健忘**:wakeup 跨边界无状态。现状 `context.py` 只在 prompt 里**给指针**("请 `read_memory(scratchpad)`"、"`mailbox_read(box=sent)` 看你发过啥"),却**不注入内容**——而 `task.goal/acceptance/checkpoint` 是**直接注入**的(`context.py:528`)。一个不靠谱的模型看到"请去读"就跳过,于是忘了自己发过什么、不看 scratchpad、不把一条主线相关的 mail 捞全。
2. **无 runtime 强制的自治**:长跑只靠 persona 约定(long-runner 自己记得 re-arm + 自限预算)。RCA 已证明 agent 自管不可靠。

## 2. 原则

- **铁律五优先**:thread 归属、loop 续命、上下文重建——全部落在 mailbox / 既有持久行上,不引入旁路控制面。
- **组合 > 新机制**:先看现有 primitive(scheduled_mail / checkpoint / park-resume / mail metadata)能不能拼出来,拼不出来才加,且加最小。
- **push,别 pull**:无状态 + 不靠谱的 agent,关键状态由 runtime **推**进 context,而不是寄希望于它主动 **拉**。

## 3. 骨架:`thread_id`(主线)——mailbox-native

一条**主线**是 owner 心智里的"一件事",会横跨多封 mail、多个 task、多个 wakeup。我们**不**从 `parent_task_id` 推导它(那是**附带连接**:谁那次 wakeup 派的就挂谁,dispatcher 每封信一个 parent=null 根 → 主线碎成多棵树,**不稳**;见 §6 拒绝项)。改为一个**有意的、机械传播的** id:

- **载体**:`thread_id: str` 放在 **mail 的 `metadata.thread_id`** 和 **task 的 `metadata.thread_id`**。**无新表**——就是信封字段,类比 `broadcast_id` / `metadata.fan_in`。
- **传播(runtime 机械执行,agent 不维护)**:
  - **播种**:owner 发信时由边缘(CLI/dashboard/channel)铸一个,或 runtime 为一条新 owner 主线铸一个。
  - **reply 继承**:`mailbox_send(reply_to=<msg>)` 自动继承被回信的 `thread_id`(在 `_mailbox_send` 出 outbox 前盖)。
  - **dispatch 继承**:`dispatch_task` 把子 task 的 `metadata.thread_id` 从**触发本 wakeup 的那封信 / 本 task** 继承。
  - **result / fan-in 继承**:子的结果邮件从其 task 继承。
  - **wakeup 知道自己的 thread**:由触发它的 mail / task 带入。
- agent **可以**显式开/拆一条主线(薄 affordance),但**不需要**——传播是机械的,这正是它在健忘模型下仍然稳的原因。

`thread_id` 同时是后面两件事的 key:**装什么上下文**(§4)、**算多少预算**(§5)。

## 4. 上下文连续性(push context)

### Stage 1 — 注入内容,不再给指针(无需 thread,最便宜,先做)

`context.py` 在 wakeup 首条 user message 里**直接注入**:

- **scratchpad 内容**(不是路径)。它本就该短(working memory),且前缀稳定可命中 prompt cache。和 checkpoint 一样 push。
- **最近 N 封 sent mail 的摘要**(标题 + recipient + 时间)。直治"忘了自己发过/承诺过什么"。

这一步消除大部分健忘,且不依赖任何新概念。

### Stage 2 — 按 thread 圈定(治"某些主线不捞全 mail")

当 wakeup 带 `thread_id` 时,runtime 额外注入**这条主线的相关 mail**(该 thread_id 下 sent + received 的近期信)以及(可选)该主线在 scratchpad 里的小节。多主线并行时,每个 wakeup 拿到的是**这条主线**的相关上下文,而不是泛泛的"最近"。

> 都是读 `mailbox_messages`(按 `metadata.thread_id` 过滤)——纯 mailbox,无旁路。

## 5. 有界自驱动:loop = **有预算的周期自邮件**

不做专门的 loop 原语/表。loop 拆开后几乎全在现有 primitive 里(`ScheduledMail` 已带 `recur_*` / `recur_until` / **`occurrence_count`**):

| loop 要素 | 现有 primitive | 增量 |
|---|---|---|
| re-arm | recurring self-mail | 无 |
| 迭代状态 | `report_progress(checkpoint=)`(自动注入下轮) | 无 |
| 轮间挂起 | PR1 `needs_input`→Phase 0.7 | 无 |
| **deadline cap** | `recur_until`(Phase -1 到点停发) | 无,已强制 |
| **iterations cap** | `occurrence_count`(已在数) | + `max_occurrences` + Phase -1 一句比较 |
| **token cap** | per-wakeup token 已记 | 按 `thread_id` 汇总该主线 wakeup 的 token |

**机制**:`mailbox_send` 的调度分支新增 `max_occurrences` / `max_tokens`。Phase -1 投递一条周期 self-mail 前先查预算(`occurrence_count >= max_occurrences` ∨ thread token 超 `max_tokens` ∨ 过 `recur_until`)→ **停止 re-arm**,并投一封 high 的 final "预算到了,收尾或升级" 邮件,让 agent 最后一个 wakeup 体面结束。

- **opt-in**:agent 给自己发一封**带预算的周期自邮件**——它本来就用自邮件做 loop,只是现在**有界**。
- **harness 强制**:caps 在调度器 Phase -1 执行,模型再想"再来一轮"也越不过自己声明的预算。与 fan-in barrier "由调度器 resolve、不让 LLM 自己数"同构。
- **token 预算挂 thread**:loop 的 token cap = 该主线所有 wakeup token 之和——thread 既是上下文 key 也是预算 key,一根骨架两用。

> 事件驱动续命(等已派子活回信 auto-wake)与定时续命(周期自邮件)并存:前者是常态,后者是没有 inbound 事件时的心跳兜底;预算 cap 覆盖二者(deadline/token 跨两种,occurrence 计周期自邮件)。

可选(事后):若实测模型不易想到这套用法,加一个**薄语法糖** `drive_self(goal, budget)`,底层就是上面这封 budgeted self-mail。**非核心。**

## 6. 明确拒绝的方案(及原因)

- **从 `parent_task_id` 推导 case/主线**:连接是附带的(挂到那次 wakeup 的 task),不是有意归属;dispatcher 每封信一个根 → 主线碎裂。只在 long-runner 子树内向下稳定,不能作通用 case。
- **专门的 loop 原语**(`driver_loops` 表 + `loop_open/loop_tick` + 新 Phase):over-machinery。现有 scheduled_mail 已 80% 覆盖(连计数器都有),只差预算 cap。
- **owner-facing `case_id` 看板**作为主目标:真正的痛点是 **agent 健忘(输入侧)**,不是缺一个进度查看器。owner 侧聚合若日后要,可直接按 `thread_id` 查 mail/task,无需新抽象。

## 7. Schema / 增量(单基线就地编辑 0001 / 0004)

- `mailbox_messages.metadata.thread_id`、`tasks.metadata.thread_id`:**复用既有 metadata 列**,无 DDL。
- `scheduled_mail`:加 `max_occurrences INTEGER`(`max_tokens INTEGER` 可选)。`occurrence_count` / `recur_until` 已存在。
- 无新表、无新调度器 Phase、无 adapter 改动。

## 8. 五铁律 / kill-test

- **铁律五(mailbox 唯一)**:thread_id 在 mail/task metadata;loop 是 self-mail;上下文重建读 `mailbox_messages`。全 mailbox-native,零旁路。**直接满足 owner "能用 mailbox 就用它"。**
- **铁律三(kill-test)**:thread_id、`occurrence_count`、预算 cap 全是已提交行;loop 状态在 checkpoint(持久)。轮间 SIGKILL → Phase -1 下个 tick 重评估,cap 仍成立,无内存态。
- **铁律一(provider 中立)**:纯 SQL + Python + mailbox,adapter 不动。
- **铁律四(持久三层)**:scratchpad/notes(全局文件)被 push 进 context;checkpoint(local-hot)承载 loop 迭代态;thread_id 在 cold-durable 的 mail/task 行。

## 9. 分 PR 路线

| PR | 内容 | 关键离线测试 |
|---|---|---|
| **T1**(Stage 1) | `context.py` 注入 scratchpad 内容 + 最近 sent mail 摘要 | wakeup prompt 含 scratchpad 内容 / 含近期 sent / 空时不炸 / 体量受限 |
| **T2**(thread 传播) | `thread_id` 铸造 + reply/dispatch/result 机械继承 | reply 继承 / dispatch 继承到子 task / 无 thread 时不破坏现状 / 不靠 agent 维护 |
| **T3**(thread 上下文) | 带 thread 的 wakeup 自动注入该主线 mail(+scratchpad 小节) | 多主线时只注入本线 / 跨 wakeup 连续 / 过滤正确 |
| **T4**(有界 loop) | `scheduled_mail.max_occurrences/max_tokens` + Phase -1 cap + final 邮件 + `mailbox_send` 参数 | 超 occurrence 停 + final 邮件 / token 超(按 thread)停 / deadline 仍生效 / 未设 cap 时行为不变 / kill 中途不丢不超 |

> 依赖序:T1 独立、立刻止血。T2 是 T3 与 T4(token 预算)的前置。T4 的 deadline/occurrence cap 不依赖 thread,可与 T2 并行;token cap 等 T2。
