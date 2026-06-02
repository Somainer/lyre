# Lyre — 确定性 Workflow 编排

> **文档定位**：定义如何把"确定性多 Agent 编排"（Claude Code 的 Workflow/Ultracode 能力集：parallel+barrier、pipeline、loop-until-dry/budget、judge panel、结构化子结果、retry、DAG）引入 Lyre——一个被设计为**禁止**同步阻塞的异步、持久、可拔线（kill-test）运行时。核心结论：编排不能是阻塞 `await`，必须**模拟为由轮询调度器评估的持久状态迁移**。本文是该设计的定稿（v2），并配套 Erlang/OTP 式的 ephemeral agent 监督/回收层。
>
> **相关**：[`FOUNDATION.md`](./FOUNDATION.md) 五条铁律；[`AGENT_RUNTIME.md`](./AGENT_RUNTIME.md) wakeup 循环与调度器；[`AGENT_CONTRACT.md`](./AGENT_CONTRACT.md) 为何删除 `await_subagents`；[`TRANSACTION_BOUNDARIES.md`](./TRANSACTION_BOUNDARIES.md) 事务边界；[`PERSISTENCE_SCHEMA.md`](./PERSISTENCE_SCHEMA.md) 持久层。
>
> **English one-liner**: A scheduler-driven, mailbox-native fan-in barrier (no blocking `await`) plus an Erlang/OTP-style supervisor + reaper for ephemeral agents — deterministic workflow patterns simulated as durable state transitions polled by the scheduler.

---

## 目录

1. [背景与中心张力](#1-背景与中心张力)
2. [设计总览](#2-设计总览)
3. [Dispatcher 不被阻塞：证明（准入判据）](#3-dispatcher-不被阻塞证明准入判据)
4. [R1：mailbox 驱动的 fan-in barrier](#4-r1mailbox-驱动的-fan-in-barrier)
5. [R2：Erlang/OTP supervisor 与 reaper](#5-r2erlangotp-supervisor-与-reaper)
6. [Schema 改动](#6-schema-改动)
7. [工具](#7-工具)
8. [调度器阶段](#8-调度器阶段)
9. [OTP 保真度映射](#9-otp-保真度映射)
10. [五铁律辩护](#10-五铁律辩护)
11. [Kill-test 演练](#11-kill-test-演练)
12. [端到端示例：5 路 judge panel](#12-端到端示例5-路-judge-panel)
13. [分 PR 路线图](#13-分-pr-路线图)
14. [明确非目标](#14-明确非目标)

> **状态**：设计已定稿；**PR1（`needs_input` park/resume + `repos.transaction()`）+ PR2（mailbox 驱动 barrier）+ PR3（`task_terminated` 邮件 / OTP monitor）已落地**（见 §13）。其余 PR4–PR6 待实现。
>
> **实现修正（PR2 落地，修复 v2 文稿的内部矛盾）**：barrier 解析后**通过一封高优先级 `system:fan-in` "ready" 邮件 + 既有 Phase 0 auto-wake 唤醒协调器**；协调器的开启-wakeup **正常 `completed`、绝不 park 进 `needs_input`**。原因正是 §10 铁律一(b)：把 dispatcher-as-coordinator park 进 `needs_input` 会令其对 Phase 0 不可见,从而**重蹈 `await_subagents` 的阻塞老路、违反 owner 准入判据**。结果邮件以 **low urgency** 静默累积(`read_unread(min_urgency='normal')` 忽略 low),只有 barrier 的 ready 邮件(high)触发唤醒,避免 partial-inbox 提前唤醒。**Mail-before-flip** 顺序(先投 ready 邮件、再 guarded 翻状态)使其 kill-safe 自愈,无需把 `insert_message` 纳入事务。
> PR1 的 `needs_input` park/resume(Phase 0.7)作为通用原语**保留**,但 barrier **刻意不用它**。下文 §3 / §7 / §11 中"`fan_in_open` park 协调器 → Phase 0.7 resume"之处,以此修正为"协调器 `completed` → fan-in-ready 邮件 → Phase 0 auto-wake"。

---

## 1. 背景与中心张力

确定性 workflow 编排（Ultracode 模型）假设**同步、进程内**的控制流：一个 driver 持有活动调用栈，发起 `parallel(thunks)`，**阻塞**在 barrier 上直到全部返回类型化值，归约后推进下一 pipeline 阶段，循环到预算/dry-round 谓词触发。**Lyre 的运行时否定了上述每一个假设**：

- **没有持调用栈的 driver**——唯一的"driver"是一个无状态 wakeup 内某个 LLM 的 tool-loop，其内存在结束时被丢弃（铁律：wakeup 跨边界无状态）。
- **没有同步 barrier**——`await_subagents` 被**有意删除**（见 `AGENT_RUNTIME.md`），因为阻塞的任务 (a) 会钉死 owner 面向的 dispatcher 单例、DoS owner，(b) park 进 `needs_input` 后会被 Phase 0 auto-wake 跳过、再也无法被更高优先级邮件打断。
- **没有进程内通道**传递类型化结果——铁律五要求每个跨 actor 的值都走持久 mailbox，且发送方永不同步观测投递。
- **拔线测试（铁律三）**禁止任何只活在进程内存里的编排状态：任意时刻 SIGKILL 后，workflow 必须能仅凭持久行恢复。

因此设计**不能"加一个阻塞 await"**，而要把 join/barrier/pipeline/loop 语义**模拟在持久 task + mailbox + lease + scheduled-mail 之上**——把同步控制流变成**由轮询调度器评估的持久状态迁移**。barrier 变成"调度器检测到某组的谓词满足 → 重新派发协调器"；loop 变成"周期性自邮件重唤协调器去检查一个持久计数器"。

**已核实的两个地基缺口**（任何方案都要先补上）：
- **没有结构化 join/barrier**：`find_children(parent_task_id)` 已定义（`sqlite_impl.py`）但调度器从不调用；`needs_input` 状态**无任何代码写入**。
- **没有强制结构化输出**：`LLMAdapter.stream_turn`（`llm_adapter.py`）没有 `tool_choice`/`response_format` 参数。

---

## 2. 设计总览

**两个持久对象 + 一个新调度器阶段 + 若干工具 + 一个可选 adapter 参数**：

- 持久的 `fan_in_groups`（纯协调契约，无 payload）+ payload-free 的 `fan_in_members`（名册：绑定 `(group_id, leg_key)` 归属与血缘）。
- 子 agent 通过既有 `mailbox_send(result_for=…)` 回传**类型化结果邮件**（发送时按 `result_schema` 校验，fail-closed）；barrier 谓词**数已投递的结果邮件行**（键在 `mailbox_messages` 到达，而非 task 完成）。
- 新调度器 **Phase 0.5** 在某组谓词满足时**原子地** resolve 该组并投递一封 digest 邮件，借**既有 Phase 0 auto-wake** 重唤协调器。
- 协调器的编排任务**在扇出后即 `completed`，永不 park 进 `needs_input`**——这是消除 `await_subagents` 第二条删除理由（Phase-0 不可见）的结构性解法。
- ephemeral 子 agent 由 **Erlang/OTP 式 supervisor + reaper**（Phase 0.6）回收：例行重启确定化（不烧 LLM），仅风暴超限才走 mailbox 升级。

**跨 wakeup / 跨 tick 的执行流**：

```
协调器 wakeup W0（普通任务 T_c，agent coord-1）:
  1. fan_in_open(kind=barrier, expect_replies=5, result_schema=R, deadline=…)
        → INSERT fan_in_groups(id=G, status='open', expect_replies=5, …)
  2. dispatch_task(fan_in_group=G) × 5   （5 个不同 agent 实例，铁律四）
        → 每个：在同一 repos.transaction() 内 INSERT tasks(child) +
                INSERT fan_in_members(G, leg_key=k, child_task_id, child_agent_id)
        → 子任务 metadata 盖章 {fan_in_group:G, leg_key:k, result_schema:R}
  3. 协调器停止调用工具 → W0 结束 → T_c 'completed' → coord-1 空闲

[若干 tick；Phase 3 按 max_concurrent_tasks 派发 5 个子任务]

子 wakeup（agent worker-k）:
  - 干活 → mailbox_send(result_for=G, leg_key=k, body=<结果>)
        发送前：校组 open → 血缘校验 → jsonschema 校验 R → 强制 recipient=coord-1
                → 盖 metadata.fan_in → 走既有 outbox（幂等 external_id）
  - 停止调用工具 → 子任务 'completed'

调度器 Phase 0.5（每 tick，介于 Phase -1 与 Phase 0 之间）:
  - find_resolvable(): COUNT(DISTINCT leg_key) >= quorum（或 deadline 过期 / loop 谓词满足）
  - try_resolve(G): UPDATE fan_in_groups SET status='quorum_met'
       WHERE id=G AND status='open' RETURNING id   （claim_lease 同款单赢家惯用法）
  - 纯 Python 组装 digest（无 LLM）→ request_resume(parent_task) 抬起 resume_ready

Phase 0.7（PR1，已落地）: needs_input -> pending（仅此处转换）
Phase 0（既有）: coord-1 有未读且无在飞任务 → auto-wake → 恢复 wakeup W1
协调器恢复 wakeup W1（全新、无状态）: 读结果邮件 / list_fan_in_groups(G) → 确定性聚合 → 报告 owner
```

"等待"只是扇出到谓词满足之间的墙钟间隙；这期间协调器任务 `completed`、agent 空闲，barrier 完全活在 `fan_in_groups` 行里。

---

## 3. Dispatcher 不被阻塞：证明（准入判据）

> 这是引入"调度器驱动 join"的**准入判据**：owner 反馈明确——`await_subagents` 当年被删就是为了解决 Dispatcher 被阻塞；只要机制不阻塞 Dispatcher 即可接受。

**消歧**：两个东西都叫 "dispatcher"——(A) **OutboxDispatcher** 单例（asyncio task）；(B) **dispatcher persona agent**（`kind:singleton`，owner 面向，唯一与 owner 对话的 actor）。本判据关心 (B)。

**两个被证伪、未被采用的论证**：
- ❌「scheduler 与 OutboxDispatcher 是不同 asyncio task 所以互不阻塞」——**错**：它们共享同一个 aiosqlite 连接（单 worker 线程串行化）。新阶段里一个长/争用的 DB 操作会与 OutboxDispatcher 的 `insert_message` 串行化。
- ❌「inline 模式串行但每个 wakeup 都会结束所以不饿死」——**错**：inline 下 `_run_task_inline` 在 `_tick` 内被同步 await，`_available_slots()` 返回 1，一个多分钟的 wakeup 阻塞整个 tick。但 inline 仅 debug 用（`serve` 默认 `--subprocess`），生产保证成立；inline 以可调试性换取该保证，如实声明。

**成立的保证（两个 horn）**：
- **阻塞 = 结构上不可能**：无 `await_subagents`、无同步 join、无 wakeup 内轮询。协调器扇出后停止调工具 → wakeup 结束。barrier 由调度器阶段解析（**不持租约、不跑 wakeup、不调 LLM、不 await 投递**）。dispatcher 的顺序-actor 槽从不被一个待决 fan-in 占用。
- **饿死 = 有界，且对单例消除**：Phase 0 只**创建** dispatcher 的待决任务,它在 Phase 3 **运行**,受 `_available_slots()` 与 `find_pending` 的 FIFO `ORDER BY created_at` 约束。在 `max_concurrent_tasks=4` 被长跑 ephemeral 子占满时,dispatcher 任务会排在更早的子之后——有界延迟饿死。Hybrid 用两层堵上：
  1. **重启风暴上限**（`bump_and_check_intensity`）：崩溃循环超预算即停止产生新派发并升级 → 待决队列不会无界增长。
  2. **为 `parent_agent_id IS NULL` 单例预留 1 个全局 slot**（PR4，~4 行）：`_available_slots()` 给 bootstrap 单例留一槽，Phase 3 优先选可运行的单例任务 → owner 面向的 dispatcher **永远有槽**。reaper/barrier 阶段不消耗 slot（只入队不运行）。

**QED**：阻塞结构上不可能；每 tick 延迟有界（索引化、批量上限、早返、单连接）；饿死被风暴上限约束、对 owner 面向单例由预留槽消除。inline（debug）模式降级为"无阻塞原语，但长 inline wakeup 会串行化 tick"——如实声明不掩盖。

---

## 4. R1：mailbox 驱动的 fan-in barrier

> **为何不走控制表**：v1 让子把结果同步写进 `fan_in_members.result_json`——一个绕过 `mailbox_messages` 的私有 inter-agent 通道（dashboard/CLI/read-state/audit/auto-wake 全盲），正是铁律五/"Lyre 是网关"所禁止。v1 这么做是为了躲 outbox 投递竞态。R1 改为：结果走 mailbox，barrier 数**已投递的结果邮件**——用"等投递到达"取代"绕过 mailbox"。

- **不新增发送工具**：子用既有 `mailbox_send(result_for=G, leg_key=k)`。`_mailbox_send` 内、入 outbox 前：(a) 载组、(b) 非 open 拒绝、(c) **血缘校验**（发送者须在名册里拥有 `(group, leg_key)`，防 CLI 伪造）、(d) `jsonschema` 校验 payload、(e) 强制 `recipient=coordinator` 并盖 `metadata.fan_in`。所有校验在入 outbox 前 `ToolError`（fail-closed）。判官投票是一等结果邮件（`metadata.fan_in.verdict`），**不用 `mailbox_react`**（它不产生 `mailbox_messages` 行、不可计数）。

- **数已投递邮件的谓词**：

```sql
-- 只读；触发是另一条 guarded UPDATE
SELECT g.id, g.quorum, g.coordinator_agent_id,
       COUNT(DISTINCT json_extract(m.metadata,'$.fan_in.leg_key')) AS delivered
FROM   fan_in_groups g
JOIN   mailbox_messages m
       ON  m.recipient = g.coordinator_agent_id
       AND json_extract(m.metadata,'$.fan_in.group_id') = g.id
WHERE  g.status = 'open'
GROUP BY g.id, g.quorum, g.coordinator_agent_id
HAVING delivered >= g.quorum;
```

- **安全性（永不早触发）**：(1) 数的是"协调器能看见"的投递事件；(2) `mailbox_messages` 唯一写者是 OutboxDispatcher 的 `insert_message`，WAL 下读已提交行无撕裂；(3) `ON CONFLICT(recipient, external_id) DO NOTHING` + `COUNT(DISTINCT leg_key)` 幂等去重；(4) 崩溃于"子完成但结果未投递"时，结果是持久未派发 outbox 行，恢复后才补齐——barrier 此前**未触发**，从不假触发；(5) 触发是 `UPDATE … WHERE status='open'` 单赢家。
- **活性（与安全性分开）**：每组 `deadline NOT NULL` + TTL 强制关闭，保证最终一定触发，尾延迟由 OutboxDispatcher poll + 调度器 poll 约束。
- **索引现实**：`CREATE INDEX mailbox_messages_fan_in ON mailbox_messages(json_extract(metadata,'$.fan_in.group_id'), recipient)`。表达式索引可能不被 planner 采用 → PR2 带 **`EXPLAIN QUERY PLAN` 离线测试**断言索引被用上，否则退化到"只扫协调器自己收件箱"的有界回退。
- **I3-纯**：每个跨 agent 结果传输都是、且仅是一条 `mailbox_messages` 行；`fan_in_groups`/`fan_in_members` 只承载契约+名册（关于一组邮件的元数据，类比 `broadcast_id`），永不承载 inter-agent payload。

---

## 5. R2：Erlang/OTP supervisor 与 reaper

**选定模型：Hybrid Reaper Phase**（评审 36 分、零致命、唯一 survives）。**例行重启确定化（不烧 LLM），仅风暴超限才走 mailbox 升级**——忠实 OTP「supervisor 是 boilerplate 而非应用逻辑」。被否的两个：SSEA（致命：依赖不存在的 `repos.transaction()`），Mailroom（致命：LLM-discretionary 重启会在 `mailbox_read` 提交 `read_at` 后 ack-and-stop 时静默丢失重启）。

- **child_spec** 是 `agents.metadata.supervision` 里的持久 JSON（零 DDL，复用既有自由列）：`{ephemeral, restart:'permanent'|'transient'|'temporary', shutdown_grace_s, max_restarts, max_seconds, fan_in_group?, restart_goal_template?, reaped_at?}`。`create_agent` 新增可选 `supervision` 参数；省略=今日行为（非 ephemeral、temporary）→ 全部既有调用点字节兼容。
- **重启强度**是**类型化**行 `supervision_state`（滑窗），由 `bump_and_check_intensity` 原子更新；超限 → 升级、**不级联杀**。用类型化表而非 `agents.metadata` JSON：`update_metadata` 是整列覆写（`sqlite_impl.py`），JSON 计数器会被撕裂。
- **DOWN / 升级 / SHUTDOWN 全走 mailbox**（铁律五），全幂等：
  - **DOWN**（信息性）：`sender='system:supervisor'`、`urgency=normal`、`external_id=down:{child}:{wakeup_id}`。每次例行重启/回收都发，审计用。
  - **ESCALATION**（决策性）：`urgency=high`（Phase 0 唤醒空闲父；MailWatcher 在飞中也能浮现）、`external_id=escalate:{child}:{window_start_at}`。**唯一的 LLM 入口**——父决定 re-plan / 重 spec / 归档 / loop owner。
  - **SHUTDOWN**：普通 `urgency=high` 邮件，子在下个 wakeup 边界读（不中途抢占，铁律四）。
- **回收 reaper**：ephemeral 子满足「重启策略不会再启 ∧ 协调义务已了结」即 `agents.archive`（幂等软删）+ `reaped_at`。reaper **从不**碰 worktree（死 wakeup 自己的 `finally` 负责）、**从不**碰活 wakeup。**安全谓词**：`find_terminated_ephemerals` 排除有活 wakeup（`ended_at IS NULL`）/在飞任务/活 subprocess 的 agent，且 reaper **先调 `close_orphans_for_task`** 再分类——复用 commits `8ac9269`/`d9d759f` 的修法，避免把崩溃子误判为 LIVE（泄漏）或把恢复中的子误判为 DOWN（双重重启）。超限子也被回收（标记终态失败，不悬空）。

---

## 6. Schema 改动

> 单基线约定：**就地编辑** `migrations/0001_initial.sql`，owner 在 schema 变更时清空本地 DB。

```sql
-- (A) tasks.resume_ready —— park/resume 闸（PR1，已落地）
--     parked('needs_input') 任务对 find_pending / find_expired_leases 不可见；
--     resume_ready=1 后由 Phase 0.7 唯一地翻回 'pending'。
ALTER ... -- 等价于在 tasks 表加列：
  resume_ready  INTEGER NOT NULL DEFAULT 0,
CREATE INDEX IF NOT EXISTS tasks_resumable
  ON tasks(resume_ready) WHERE status = 'needs_input';

-- (B) agents.status / agents.metadata 不变：status 仍只 idle|archived；
--     'ephemeral' 由 metadata.supervision 派生，无新生命周期值。

-- (C) supervision_state —— 类型化重启强度滑窗（torn-write 安全）
CREATE TABLE IF NOT EXISTS supervision_state (
  agent_id        TEXT PRIMARY KEY REFERENCES agents(id),
  restart_count   INTEGER NOT NULL DEFAULT 0,
  window_start_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  last_restart_at TEXT,
  last_reason     TEXT,
  escalated_at    TEXT
);

-- (D) fan_in_groups —— 纯协调契约（无 payload），deadline NOT NULL（活性）
CREATE TABLE IF NOT EXISTS fan_in_groups (
  id                   TEXT PRIMARY KEY,
  coordinator_agent_id TEXT NOT NULL REFERENCES agents(id),
  parent_task_id       TEXT REFERENCES tasks(id),
  expect_replies       INTEGER NOT NULL,
  quorum               INTEGER NOT NULL,
  result_schema        TEXT NOT NULL,
  budget_tokens        INTEGER,
  dry_round            INTEGER NOT NULL DEFAULT 0,
  deadline             TEXT NOT NULL,
  status               TEXT NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open','quorum_met','expired','cancelled','resolved')),
  created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  resolved_at          TEXT
);
CREATE INDEX IF NOT EXISTS fan_in_groups_open
  ON fan_in_groups(status, deadline) WHERE status='open';

-- (E) fan_in_members —— payload-free 名册：绑定 (group, leg_key) 归属 + 血缘
CREATE TABLE IF NOT EXISTS fan_in_members (
  group_id       TEXT NOT NULL REFERENCES fan_in_groups(id),
  leg_key        INTEGER NOT NULL,
  child_task_id  TEXT NOT NULL REFERENCES tasks(id),
  child_agent_id TEXT NOT NULL REFERENCES agents(id),
  PRIMARY KEY (group_id, leg_key)   -- 结构上防重复槽 under-count
);

-- (F) barrier JOIN 表达式索引（PR2 配 EXPLAIN 测试，否则有界回退）
CREATE INDEX IF NOT EXISTS mailbox_messages_fan_in
  ON mailbox_messages(json_extract(metadata,'$.fan_in.group_id'), recipient);

-- mailbox_messages 列不变（信封放既有 metadata）；outbox.kind 不新增。
```

---

## 7. 工具

| 工具 | 状态 | 行为 | allowlist |
|---|---|---|---|
| `mailbox_send` | 改 | 加 `result_for/leg_key`；设了则载组→非 open 拒→血缘校验→`jsonschema` 校验→强制 recipient=协调器→盖 `metadata.fan_in`，全部入 outbox 前 `ToolError`（fail-closed） | 全体（既有） |
| `fan_in_open` | 新 | 协调器专用，INSERT `fan_in_groups`(open) 返回 `{group_id}`；`deadline` 必填（活性） | coordinator/dispatcher/leader |
| `fan_in_status` | 新 | 只读：`{expect, quorum, delivered, status}`（数邮件非任务） | 同上 |
| `fan_in_cancel` | 新 | `status='cancelled'`，晚到结果退普通收件箱 | 同上 |
| `create_agent` | 改 | 加可选 `supervision`，省略=今日行为 | 既有 |
| `dispatch_task` | 改 | 加 `fan_in:{group_id,leg_key}`，写名册行 + 盖子 `supervision.ephemeral` | 既有 |
| `submit_result` | **删** | 被 `mailbox_send(result_for=…)` 取代 | — |
| `await_subagents` | **永删** | 永不复活（铁律一） | — |
| restart/reap/DOWN | 无 | 纯调度器 boilerplate，非 agent 可调（OTP：supervisor 不是应用代码） | n/a |

PR1 另加两个**内部转换**（非工具）：park（`task → needs_input`）与 resume（`needs_input → pending`，仅调度器）。

---

## 8. 调度器阶段

`_tick`（`scheduler.py`）最终顺序。`repos.transaction()`（PR1）是多行监督写原子提交的前置——已核实今天**不存在**该原语（每个 DAO 独立 commit）。

```
Phase -1   _deliver_scheduled_mail()                  [已有]
Phase 0    _auto_dispatch_for_unread_mail()           [已有] —— find_active_for_persona
                                                        含 needs_input，故 parked 协调器不被 auto-wake
Phase 0.5  _resolve_fan_in_barriers()    [PR2] —— 无 open 组则早返
Phase 0.6  _supervise_and_reap()         [PR4] —— 无活 ephemeral 则早返
Phase 0.7  _resume_parked_tasks()        [PR1，已落地] —— needs_input -> pending（唯一写者）
Phase 2    find_expired_leases (in_progress)          [已有，slot 受限]
Phase 3    find_pending (pending)                     [已有，slot 受限，PR4 预留单例槽]
```

```python
# Phase 0.5 —— 有界、索引化、无 LLM、无租约。谓词：COUNT(DISTINCT leg_key)。
async def _resolve_fan_in_barriers(self):
    if not await self.repos.fan_in.any_open():   # 早返
        return
    for g in await self.repos.fan_in.find_open(limit=20):
        async with self.repos.transaction():     # PR1 原子缝
            if g.deadline and now() > g.deadline:
                await self.repos.fan_in.set_status(g.id, 'expired', guard='open')
                await self.repos.tasks.request_resume(g.parent_task_id)
                continue
            delivered = await self.repos.mailbox.count_fan_in_results(
                g.coordinator_agent_id, g.id)     # §4 谓词，EXPLAIN 验证索引
            if delivered >= g.quorum:
                if await self.repos.fan_in.set_status(g.id, 'quorum_met', guard='open'):
                    await self.repos.tasks.request_resume(g.parent_task_id)
    # TTL 强关（活性）：超 LYRE_FANIN_MAX_AGE 的 open 组置 expired。

# Phase 0.7 —— PR1 已落地，唯一的 needs_input -> pending 写者。
async def _resume_parked_tasks(self):
    for t in await self.repos.tasks.find_resumable(limit=20):
        if await self.repos.tasks.resume(t.id):
            log.info("scheduler_resumed_parked_task", task_id=t.id)
```

**`_classify_exit` 与 Phase-2 的衔接**：被 SIGKILL 的子无退出码——表现为过期租约由 Phase 2 恢复，而非干净终态。故 reaper 只在子任务**终态**（经 `close_orphans_for_task` 后）才判 DOWN，绝不基于裸 open-wakeup 或未过期租约；ephemeral 子的 Phase 2 恢复也调 `bump_and_check_intensity`，使纯靠租约过期恢复的崩溃循环仍撞风暴上限。

---

## 9. OTP 保真度映射

| OTP 概念 | Lyre 映射 | 保真度 |
|---|---|---|
| 监督树 | `agents.parent_agent_id`；NULL=钉死 root | clean |
| `child_spec {start,restart,shutdown}` | `agents.metadata.supervision` JSON | clean |
| 动态子（`simple_one_for_one`） | `create_agent`+`dispatch_task` 按需——Lyre 原生模式 | clean |
| 静态 permanent 子 | NULL-parent bootstrap 单例 | clean |
| restart 类型 permanent/transient/temporary | reaper 在终态任务上解释 | clean |
| 重启强度 MaxR/MaxT | 类型化 `supervision_state` 滑窗；超限→升级**不级联** | clean（语义改为升级） |
| `monitor`+`DOWN` | `task_terminated` 邮件，幂等 external_id——比 in-VM DOWN 更强（跨崩溃存活） | clean |
| supervisor=进程 | 拆分：策略在 DB 行，机制在轮询调度器 reaper（铁律五，放 wakeup 里会在 DOWN→下次唤醒间丢失重启） | partial |
| `link`（双向致命） | 单向非致命 monitor（`context.py:262-271` 的"向 parent escalate"是父→子半边） | partial（故意降级） |
| `shutdown` grace | `archive_agent` + wakeup 边界 drain；reaper 等租约过期 | partial |
| `one_for_all`/`rest_for_one` | **不做调度器原语**——需同步抢占持久轮询 actor，违铁律四；降为协调器 mailbox 策略 | partial |
| `brutal_kill` inline wakeup | 无对应——会撕裂 append-only FS 写；OS SIGKILL+租约恢复（subprocess）是唯一可拔线类比 | poor（在 inline 层拒绝） |
| 级联监督者死亡 | **拒绝**——root 是钉死单例；超限→升级邮件，绝不自动拆除 | poor（故意分歧） |

---

## 10. 五铁律辩护

- **铁律一（无阻塞 await）——直接回答 owner 准入判据**：无 `await_subagents`、无 join 工具、无 wakeup 内轮询。barrier 是确定性调度器阶段（无租约、无 wakeup、无 LLM）；协调器扇出后结束 wakeup，仅由 Phase 0.7 resume + Phase 0/3 重唤。预留单例槽（PR4）约束饿死。详见 §3。
- **铁律三（拔线）**：每个事实都是已提交行：`fan_in_groups.status`、`supervision_state`（类型化、torn-write 安全）、`agents.metadata.supervision`、`archived_at`、`reaped_at`、`resume_ready`、持久 outbox 行。多行监督写由 `repos.transaction()`（PR1）原子化。详见 §11。
- **铁律五（mailbox-only）——直接满足 owner "mailbox 驱动一切"**：结果、DOWN、升级、SHUTDOWN、barrier 重唤全是 `mailbox_messages` 行；v1 的 `result_json` 旁路被删。`fan_in_groups`/`fan_in_members` 承载契约+名册（关于邮件的元数据，类比 `broadcast_id`），永不承载 inter-agent payload。**信任模型**：barrier 只数发送者在名册里拥有 `(group, leg_key)` 的结果邮件（发送时 + 计数时双重血缘校验），owner/CLI（合法 I3 客户端）无法伪造 leg 或假冒 DOWN。
- **铁律四（顺序 actor）**：`one_for_one` 重启只造一个待决任务，受 `has_active_for_agent` + `claimed_in_this_tick` 约束；`one_for_all`/`rest_for_one` 因会违此律而被拒。SHUTDOWN 在下个边界读，不抢占。
- **wakeup 无状态**：无 fan-in/监督状态存于被丢弃的 messages list；全在 DB 行 + mailbox，每 tick/wakeup 重读。重启机制在调度器、不在 supervisor 内存——正是为防 SIGKILL 在 DOWN→下次 wakeup 间丢失。
- **Provider 中立**：零 `adapter/` 改动。结构化校验是纯 Python `jsonschema`；barrier/reaper/supervisor 是 SQL+Python。唯一 LLM 调用是升级触发的父 wakeup，走既有 router/adapter 缝。可选 `tool_choice`（`ToolChoice` dataclass）是延迟优化、非正确性依赖，各 adapter 机械翻译、无编排词泄漏。

---

## 11. Kill-test 演练

- **子完成↔结果邮件投递之间 SIGKILL**：barrier 数已投递邮件，故 kill 瞬间 `delivered=4 < quorum=5` **未假触发**（正是 v1 的精确故障）；重启后 dispatcher 按幂等 `external_id` 补投，count→5，Phase 0.5 单赢家触发一次，Phase 0.7 翻 `needs_input→pending`，协调器读到 5 封真实邮件。精确-N、精确一次。
- **reaper 撞活 wakeup**：`find_terminated_ephemerals` 排除活 wakeup/在飞任务/活 subprocess，且先 `close_orphans_for_task` 再分类——崩溃子不被误判 LIVE（泄漏），恢复子不被误判 DOWN（双重重启）。最坏情况软删一个待重启子：`dispatch_task` 复查 `agent.status=='archived'` → `ToolError`，supervisor 重建（one_for_one 复用同 `agent_id`，监督树/mailbox 不悬空）；软删可逆，错删不丢数据。
- **DOWN 邮件重投致双重重启**：重启**不**由邮件触发，而由 reaper 从已提交终态行重导，受 `supervision_state.restart_count` + wakeup 水印门控、全在一个 `repos.transaction()`。重投 DOWN 撞 `UNIQUE(recipient, external_id)` → no-op。至少一次邮件不会膨胀重启。
- **reap 中途 SIGKILL**：固定写序于一个事务内：bump+水印 → restart-INSERT(或 archive) → outbox DOWN。整块一次提交：要么回滚（下次重导相同 DOWN 幂等重试），要么全提交（水印已进，重投跳过）。`archive` 幂等（`WHERE status!='archived'`）。子绝不会同时既未重启又未回收又未升级。
- **跨层 kill**（既是 fan-in 成员又是 ephemeral）：结果邮件 `external_id` 键于 `(wakeup_id, tool_use_id, recipient)` 而非 agent_id，重启子用新 `external_id` 重发同 leg → `COUNT(DISTINCT leg_key)` 折叠为一次；one_for_one 复用同 `agent_id` → 名册血缘校验仍匹配；重启受终态门控（经 `close_orphans_for_task`）→ 结果重放与重启不同时触发。PR4 专测。

**诚实的确定性契约**：**控制面**确定（谓词/tally/推进是已提交行的纯函数，可重放）；**工作本身**不确定（被重跑的判官可能翻票）。保证是"对落地判决做确定性 tally"，不是"崩溃重放后逐字节相同的面板结果"。

---

## 12. 端到端示例：5 路 judge panel

1. 协调器 `dispatcher-1` `fan_in_open(expect_replies=5, quorum=4, result_schema={verdict,rationale}, deadline="+10m")` → `G`，再 `create_agent`+`dispatch_task` × 5 个 ephemeral reviewer（`supervision={ephemeral, restart:transient, max_restarts:2, max_seconds:120, fan_in_group:G}`），各写名册 `(G, leg_key=1..5)`。然后 park `dispatcher-1` 任务 → `needs_input`，停止调工具，wakeup 结束。**owner 邮件仍能经 Phase 0 唤醒**（needs_input 只抑制这个 parked 任务，且 PR4 预留槽保证 Phase-3 槽）。
2. reviewer a/b/d/e 完成 → `mailbox_send(result_for=G, leg_key=N, …)`：血缘校验、schema 校验、盖 `metadata.fan_in`、入 outbox、投递进 `dispatcher-1` 收件箱。这些是 normal 邮件，但 `dispatcher-1` parked，`find_active_for_persona`（含 needs_input）抑制 auto-wake 进半空收件箱——barrier-bypass 洞被 PR1 的 park 关闭。
3. reviewer-c 崩溃（SIGKILL）→ 租约过期 → Phase 2 恢复 → 再失败。Phase 0.6 `close_orphans_for_task`、判 abnormal、`bump_and_check_intensity` 返 ok(1/2)、重派 c 的 leg（one_for_one）、发 DOWN（normal）。重启的 c（同 `agent_id`）成功，用新 `external_id` 发结果。
4. Phase 0.5：`COUNT(DISTINCT leg_key)` 达 4 → `quorum_met`（单赢家）；Phase 0.7 翻 `dispatcher-1` `needs_input→pending`；Phase 3 认领；协调器读 5 封结果邮件聚合裁决。
5. Phase 0.6 回收：a–e 终态、组终态、策略不再启 → `archive`+`reaped_at`，各发 DOWN(reaped) 审计邮件。ephemeral panel 消失；mailbox/transcript 历史保留（软删）。
6. *风暴变体*：若 c 在 120s 内崩溃第 3 次，`bump_and_check_intensity` 返 false → reaper 置 `escalated_at`、标 c 任务 `failed`（被回收，不悬空）、发一封 `urgency=high` 升级邮件给 `dispatcher-1`。**仅此邮件**唤醒协调器 LLM 决策。常态（步骤 1–5）从不为监督触碰 LLM。

---

## 13. 分 PR 路线图

| PR | 内容 | 关键离线测试 |
|---|---|---|
| **PR1** ✅ **已落地** | `needs_input` park/resume（`tasks.resume_ready` 列 + `park`/`request_resume`/`find_resumable`/`resume` + Phase 0.7）+ `repos.transaction()` | `test_park_hides_task_from_find_pending_and_expired_leases` / `test_resume_is_guarded_and_idempotent_across_sigkill` / `test_parked_task_suppresses_auto_wake_for_its_agent` / `test_transaction_rolls_back_partial_supervisory_write` 等 9 个，全绿 |
| **PR2** | R1 mailbox barrier：`fan_in_groups`+名册+`mailbox_send` 校验/血缘钩子+`fan_in_open/status/cancel`+Phase 0.5+表达式索引 | 数已投递非完成 / outbox 未投递时不早触发 / 发送时校验 fail-closed / 伪造 CLI 结果被血缘拒 / `EXPLAIN` 用上索引 / deadline 过期恢复协调器 |
| **PR3** ✅ **已落地** | `task_terminated` 邮件(OTP monitor 类比):终态任务在 end-of-wakeup → 通知 supervisor(`parent_task` 的 agent → `owner` 回退),`metadata.kind` 供模式匹配,幂等 `external_id=task_terminated:<id>`。**重新实现而非合并 `ce27a39`** —— 后者依赖未合并的 #29(结构化 `failure_reason`/`awaiting`/`_resolve_end_statuses`)且基于 pre-PR1 main,直接合并会拖入额外未合并 PR 并冲突。**两条抑制规则**:fan-in 成员(PR2 barrier 已聚合,否则会用 normal urgency 提前唤醒协调器)+ `auto_dispatched`(内部 inbox 任务)跳过;top-level 任务仅**失败**时通知 owner(成功由 agent 自己回信,系统 ping 是噪音) | child 完成→parent / child 失败→high+reason / top-level 失败→owner / top-level 完成→静默 / fan-in 成员→静默 / auto_dispatched→静默 / parent 归档→回退 owner / 幂等 external_id / 非终态+None→静默 / `_tick` 集成,共 10 个，全绿 |
| **PR4** | R2 supervisor+reaper：`supervision_state`、Phase 0.6、`create_agent`/`dispatch_task` supervision 参数、`close_orphans_for_task` 入 liveness 谓词、预留单例槽 | reaper 跳过活 wakeup / 先关孤儿 wakeup / 风暴上限+升级 / 超限子被回收 / Phase2 恢复也计强度 / 预留槽使 dispatcher 饱和下仍可运行 / 跨层 kill |
| **PR5** | 升级处理 + persona 文档（dispatcher/leader 聚合结果邮件 + 处理升级） | 升级邮件 high 且每窗一发 / 协调器 resume 时聚合 |
| **PR6** | TTL 强关 + 可观测性（`list_agents`/dashboard 显示 reaped/storm_halted） | TTL 关闭泄漏 open 组 / dashboard 显示 reaped/storm_halted |

> **依赖序**：PR1 是硬地基（barrier resume 与 supervisor escalation 都终结于 `needs_input→pending`，而该转换 main 上不存在）。PR3 可与 PR2 并行（已有原型分支）。

---

## 14. 明确非目标

- **不做声明式 workflow DSL / plan graph / 运行时强制的 strategy 枚举**（owner：声明式 DSL 是 overdesign）。唯一声明式构件是一个小契约行 + 一个 `child_spec` JSON blob；**每条流程都 mailbox 驱动**。
- **不复活 `await_subagents` / 阻塞 join**——铁律一；dispatcher-不阻塞证明依赖其缺席。
- **OTP 中不采纳**：`one_for_all`/`rest_for_one` 作调度器原语（不能同步抢占持久轮询 actor，铁律四；仅作协调器 mailbox 策略）；`brutal_kill` inline wakeup（撕裂 append-only FS 写）；双向致命 link + `trap_exit`（无致命信号可 trap）；级联监督者死亡（root 是钉死单例 → 升级，绝不自动拆除）。
- **除 `supervision_state` 外不新增监督表**（仅因 torn-write 安全而类型化）；监督模型其余靠 `agents.metadata` + `parent_agent_id`，守单基线迁移。
- **本范围内不做被回收 agent 的冷归档/压缩**——软删行像今日 `archive_agent` 一样累积；约束它是单独的 PERSISTENCE_SCHEMA 议题。

---

> **研究产物**（仓库外，供追溯）：`/tmp/lyre-research/` 下有架构简报 `BRIEF.md`、9 份子系统地图 `MAPS.json`、v1/v2 设计全文与评分板/对抗批判 JSON。本设计由三个多 Agent workflow（共 59 个 agent、~450 万 token）产出，所有承重代码声明均逐一对照源码核实。
