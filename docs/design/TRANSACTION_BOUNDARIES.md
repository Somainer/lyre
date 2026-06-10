# Lyre — 事务边界设计

> **文档定位**：定义"一次 agent 唤醒"从读持久层到写回的精确事务边界，确保拔线测试通过。
> **相关**：[`FOUNDATION.md §4`](./FOUNDATION.md#4-工程后果拔线测试的三条硬约束) 拔线测试的三条硬约束；[`AGENT_CONTRACT.md`](./AGENT_CONTRACT.md) 接口契约。
>
> **Implementation correction (2026-06-10)**: the buffered-send / single Step-9 commit-point model in this doc is **historical**. As built, every effectful tool call commits durably **at tool time**: `mailbox_send` enqueues outbox rows immediately (`src/lyre/runtime/tools/mailbox.py`, external_id = `{wakeup_id}:{tool_use_id}:{recipient}`); `read_at` is written immediately (§6.2 already reflects this); `report_progress` checkpoints immediately. Recovery = per-write durability + idempotent `external_id` dedup + one **fenced end-of-wakeup finalize** transaction (wakeups.end + task status fenced on the holder wakeup + `task_terminated` outbox row). Consequence: a mid-wakeup kill does **not** mean "本次唤醒等于没发生" — already-sent mail stands and gets dispatched, and a lease-expiry re-run re-sends with fresh wakeup-scoped external_ids: **at-least-once** mail semantics, accepted. §2's 关键定理, the §3 kill-point tables ("Mailbox | 未发") and §6.1 are wrong on these points. Also historical: Postgres (SQLite shipped), fork-subprocess / tmpdir / ssh-agent (see FOUNDATION.md §3.3 note). As-built model: `RUNTIME_CURRENT.md`.

---

## 目录

1. [设计驱动力：拔线测试](#1-设计驱动力拔线测试)
2. [一次 agent 唤醒的精确步骤](#2-一次-agent-唤醒的精确步骤)
3. [四个 kill 点的行为分析](#3-四个-kill-点的行为分析)
4. [Outbox 模式（跨存储原子提交）](#4-outbox-模式跨存储原子提交)
5. [幂等性设计](#5-幂等性设计)
6. [Mailbox 在事务中的位置](#6-mailbox-在事务中的位置)
7. [任务续做协议](#7-任务续做协议)
8. [失败模式目录](#8-失败模式目录)
9. [v0.3 已识别但待解决的问题](#9-v03-已识别但待解决的问题)

---

## 1. 设计驱动力：拔线测试

拔线测试约束：

- **4 个 kill 点**：
  1. context 装配完成、动手前
  2. 编辑进行中
  3. 编辑完成、提交前
  4. 提交后、事件未发出前
- **三条通过标准（全要）**：
  1. 重启后状态完全可重建
  2. 操作幂等
  3. 任务可续做

本文档的所有设计选择**直接服务于这三条**。任何不能在四个 kill 点上同时满足三条标准的设计，判错。

> **术语说明**：本文档说"agent"指 Lyre 中任何角色的执行实体（leader / worker / reviewer 等），它们共用同一抽象（[`AGENT_CONTRACT.md`](./AGENT_CONTRACT.md) §1）。事务边界对所有 persona 一视同仁。

---

## 2. 一次 agent 唤醒的精确步骤

```
[Step 1] Wakeup                调度器触发；分配 task_id；Lyre 主进程 fork agent subprocess
                               Subprocess cwd = per-task tmpdir，env = 清洗后的最小 env，
                               SSH_AUTH_SOCK 指向 task-local ssh-agent（挂 per-task ephemeral key）
[Step 2] Acquire lease         在持久层 (Postgres tasks.lease_until) 获取租约
                               Lease 时长 = task.lease_duration（per-task 可配，默认 30 min）
                               同任务同时只有一个 agent 持 lease
[Step 3] Load persona          从 global 读 persona spec（只读，无副作用）
[Step 4] Read checkpoint       从持久层加载最近 checkpoint（首次唤醒为 null）
[Step 5] Assemble context      按 FOUNDATION §3.5 四步装配（只读，无副作用）
[Step 6] Read inbox            mailbox_read 拉 unread（read_at IS NULL）+ 立即写 read_at
                               （0005 之后 per-message 而非 cursor）
[Step 7] Execute loop          思考 → tool / shell call → observe → 重复
                               期间可在 subprocess 内 shell 自由执行（write_file、git_commit、
                               git_push、open PR 等）—— 产生进程内 / 远端副作用
                               可调 Lyre 工具（mailbox_send、report_progress、
                               report_side_effect 等，走 Unix socket gateway，进 outbox buffer）
[Step 8] Prepare outputs       本地组装 artifacts blob、emitted messages、新 checkpoint、
                               self_reported_side_effects 清单
[Step 9] *** COMMIT POINT ***  单一 DB 事务，原子写入以下到 Postgres：
                               - 任务进度推进 (tasks.checkpoint = 新值)
                               - artifacts 引用 (artifacts 行)
                               - outbox 行 (mailbox 消息派生 + Tier 1 检测派生)
                               - mailbox 读偏移推进 (mailboxes.last_processed)
                               - lease 释放或刷新
                               Artifact blob 本体若大于阈值，先写 object store
                               (用内容哈希命名)；DB 事务里只写引用
[Step 10] Dispatcher takes over  outbox dispatcher 进程异步扫描 outbox：
                               - 投递 mailbox 消息到接收方持久 mailbox
                               - 派生 Tier 1 通知到 owner mailbox（基于 self_reported_side_effects）
                               每条标记 dispatched_at；幂等可重试
[Step 11] Terminate            Agent subprocess 退出；Lyre 主进程 reap 子进程
                               rm -rf tmpdir、kill task-local ssh-agent、异步撤公钥
                               （Tmpdir 内的 worktree、ephemeral SSH key、所有临时文件全部清理）
```

**关键定理**：

> Step 1-8 任何时点 kill：subprocess 终止 + tmpdir 由 Lyre rm -rf（kill 时尚未到 Step 11 也会被下次 cleanup loop 兜底）；worktree 与未上报的所有未持久副作用全部消失，本次唤醒等于没发生；新 agent 接 lease 后从 checkpoint 重做。
>
> Step 9 完成后：所有"已 commit 的工作"持久；Step 10 由独立 dispatcher 承担，dispatcher 死亡可重启续做。

> ⚠️ **Correction (2026-06-10)**: superseded — mail / read-state / checkpoint commit mid-wakeup at tool time; Step 9 survives only as the fenced status-finalize transaction. See the banner at the top.

**一个重要变化**（相对 v0.1）：v0.2 起 Agent 通过 subprocess 内 shell 直接做 git push / open PR 等 Tier 1 操作，**这些操作的"外部副作用已在远端当场发生"**（不像 v0.1 那样进 outbox 等异步投递）。Lyre 通过 agent 调 `report_side_effect` 自报的方式知晓这些副作用，并在 COMMIT POINT 把"派生通知"写进 outbox 等待 dispatcher 投递给 owner。详见 §3 kill 点 4 与 §4。

---

## 3. 四个 kill 点的行为分析

逐点拆 Q5 定的 4 个 kill 点。

### Kill 点 1：context 装配完成、动手前

| 维度 | 状态 |
|---|---|
| 持久层 | lease 已持有，checkpoint 是初始或上次的；**无新增副作用** |
| Container | 已启动，worktree 是空 ephemeral clone |
| Mailbox（接收方）| 未发任何消息 |
| Git 远端 | 无任何 push |

**重启行为**：调度器周期扫 `lease_until < now` 的任务；Lyre 检测 subprocess 死亡 (waitpid 或 zombie reaping)，cleanup loop 把 tmpdir rm -rf；新 agent subprocess 启动 + 新 tmpdir + 重新 git clone；从 Step 2 走起；context 重装（context 是 stateless 的，重装一致）。等价于没拔过线。

✓ 可重建 ✓ 幂等（重装 context 无副作用）✓ 可续做（实际上是从头）

### Kill 点 2：编辑进行中

| 维度 | 状态 |
|---|---|
| 持久层 | lease 已持有，checkpoint 未推进 |
| Container | 有 in-flight 修改；worker 可能已 git commit 到本地分支 |
| Mailbox（接收方）| 未发（outbox buffer 在 agent 内存，未提交）|
| Git 远端 | 无任何 push |

> ⚠️ **Correction (2026-06-10)**: the Mailbox row is wrong as built — any `mailbox_send` already made by this point is durably committed to outbox and WILL be delivered; the re-run may re-send (at-least-once). See top banner.

**重启行为**：新 agent 接 lease；**tmpdir 不继承**——旧 tmpdir 已被 rm -rf；新 subprocess 启动 + 新 tmpdir + 重新 git clone；本地 worktree 修改 / 本地 commit 全部消失（因为它们只在销毁的 tmpdir 里）；从 checkpoint 重做这次任务的"这一步"。

> ⚠️ 关键约定：**tmpdir 与 subprocess in-memory 状态都不是持久状态**。Agent 不能假设本地 worktree、本地 commit、临时文件在重启后保留。需要保留的中间产物必须 `report_progress` 写到 checkpoint，由 Step 9 持久化。

✓ 可重建（checkpoint 完整）✓ 幂等（重做未 push / 未自报的步骤无外部副作用）✓ 可续做

### Kill 点 3：编辑完成、提交前

| 维度 | 状态 |
|---|---|
| 持久层 | 同上 |
| Tmpdir / subprocess | 编辑完成、本地 commit 完成，**可能已 git push 远端 + 已开 PR**（Tier 1 操作发生在 subprocess 内）|
| Mailbox（接收方）| 未发 |
| Git 远端 | **可能已有 push 的分支 + 已开的 PR**（如果 agent 在 kill 前完成了 push / open_pr 操作但还没到 Step 9） |

> ⚠️ **Correction (2026-06-10)**: the Mailbox row is wrong as built — mail sent before the kill is already durable in outbox and WILL be delivered; the re-run may re-send (at-least-once). See top banner.

**这是 v0.2 起引入的复杂情形**——v0.1 假设所有副作用都过 Lyre 工具 outbox，因此可以"全部回滚"。v0.2 起 agent 通过 subprocess 内 shell 直接做 git push / open PR，**这些副作用已经留在 git hosting 上无法回滚**。

**重启行为**：
1. 新 agent 接 lease，新 subprocess 启动 + 新 tmpdir + 重新 git clone
2. 从 checkpoint 重做"这一步"
3. **Agent 必须先观察远端状态**：检查目标分支是否已存在（push 已发生 → 内容相同则跳过、不同则决定 force push 或重新 push 新内容）、检查 PR 是否已存在（已开 → 跳过 `open_pr`；不存在 → 重做 open_pr）
4. 同样地，最终调 `report_side_effect` 让 Lyre 知晓

**幂等的保证靠 git 自身**：
- `git push <branch>` 同内容是 no-op
- `gh pr create` 同 task_id 重复执行——agent 应先 query 是否存在，存在则跳过

**关键约定**：Tier 1 操作的"幂等责任"在 agent 这一侧（agent prompt 应明确"操作前先查重"），不在 Lyre。Lyre 仅在 dispatcher 投递 Tier 1 通知时按 `external_id` 去重。

✓ 可重建（持久层未受影响；git 远端有可见副作用但可被发现）
✓ 幂等（git push 天然幂等；PR 创建靠 agent 查重）
✓ 可续做

> 这是 v0.2 相对 v0.1 的**主要语义弱化**：Tier 1 操作不再"事务回滚"，而是"重启后由 agent 重新观察现状续做"。owner 承担了这一弱化（[`AGENT_CONTRACT.md §4.5`](./AGENT_CONTRACT.md#45-tier-矩阵的执行open_questions-q3)）。

### Kill 点 4：提交后、事件未发出前

| 维度 | 状态 |
|---|---|
| 持久层 | Step 9 已完成（任务进度、artifacts、outbox 行已写入） |
| Container | 销毁中或已销毁 |
| Mailbox（接收方）| outbox 行存在但 `dispatched_at` 为 NULL，接收方 mailbox 还看不到 |
| Git 远端 | 已 push / 已开 PR（subprocess 内已发生） |

**重启行为**：agent 不必重启——任务已推进。**Outbox dispatcher** 独立运行；它周期性扫 `dispatched_at IS NULL` 的行，执行真正的投递：

- **Mailbox 消息**：插入接收方 mailbox 表（用 `external_id` 去重，至少一次投递）
- **Tier 1 派生通知**：基于 `self_reported_side_effects`，向 owner mailbox 派生 `urgency=normal` 消息（如"agent 已 push 分支 lyre/task-X/foo / 已开 PR #42"）

Dispatcher 自身 kill 后由进程管理器重启，从 outbox 未投递条目续做。

✓ 可重建（持久层已含所有真相）✓ 幂等（dispatcher 按 `external_id` 去重）✓ 可续做（dispatcher 续做投递，agent 不再回头）

---

## 4. Outbox 模式（跨存储原子提交）

> [`FOUNDATION.md §4`](./FOUNDATION.md#4-工程后果拔线测试的三条硬约束) 第一条：跨存储写入必须事务性原子。

### 4.1 为什么需要 outbox

Step 9 涉及多种存储：

- Postgres：任务进度、artifacts 引用、读偏移、lease
- Object store：大型 artifact blob
- 其它 actor 的 mailbox：消息投递（持久化在 Postgres 的 mailbox 表，但需要 dispatcher 投递）
- Owner mailbox：Tier 1 派生通知（同上）

**注意 v0.2 起的关键差异**：git push / open PR 等 subprocess 内副作用**不进 outbox**——它们在 subprocess 内已经发生并直接命中远端。Outbox 只承担 Lyre 持久层内部的"派生通知"投递。

要让"任务进度 + artifacts 引用 + outbox 行"原子提交，沿用单一 Postgres 事务策略。

### 4.2 Outbox 表 schema 草案

```sql
CREATE TABLE outbox (
  id              BIGSERIAL PRIMARY KEY,
  task_id         UUID NOT NULL,
  wakeup_id       UUID NOT NULL,            -- 哪一次唤醒产生的
  kind            TEXT NOT NULL,            -- "mailbox_send" / "tier1_notification"
  payload         JSONB NOT NULL,           -- 副作用的参数
  external_id     TEXT NOT NULL,            -- dispatcher 用它幂等
  created_at      TIMESTAMPTZ NOT NULL,
  dispatched_at   TIMESTAMPTZ,              -- NULL = 未投递
  dispatch_attempts INT NOT NULL DEFAULT 0,
  last_error      TEXT,

  UNIQUE (kind, external_id)                -- 防止 agent 重做造成重复 outbox 行
);

CREATE INDEX outbox_undispatched ON outbox (created_at) WHERE dispatched_at IS NULL;
```

**`external_id` 的构造规则**：

- `mailbox_send`：`{wakeup_id}:{outbox_seq}`——同一唤醒同一 seq 重做时 ON CONFLICT DO NOTHING
- `tier1_notification`：`{task_id}:{side_effect_kind}:{side_effect_hash}`——同任务同副作用只通知一次（如同一 PR 多次自报不会重复通知 owner）

### 4.3 Dispatcher 行为

```
loop:
  rows = SELECT * FROM outbox 
         WHERE dispatched_at IS NULL 
         ORDER BY created_at 
         LIMIT 100
  for row in rows:
    try:
      execute_side_effect(row)              # 幂等：按 external_id 去重
      UPDATE outbox 
      SET dispatched_at = now() 
      WHERE id = row.id
    except RetryableError:
      UPDATE outbox 
      SET dispatch_attempts = attempts + 1, last_error = err 
      WHERE id = row.id
      # 不更新 dispatched_at；下次 loop 重试
    except PermanentError:
      # 发 urgency=blocker 给 leader-persona agent / owner
      ...
  sleep(short_interval)
```

Dispatcher 是独立进程；可水平扩展（按 `task_id` 分片避免同任务并行投递）。

---

## 5. 幂等性设计

> [`FOUNDATION.md §4`](./FOUNDATION.md#4-工程后果拔线测试的三条硬约束) 第三条：所有 agent 动作必须幂等。

### 5.1 Container 内副作用（由 agent 自身负责幂等）

| 副作用 | 幂等机制 | 责任方 |
|---|---|---|
| Git push 工作分支 | 分支名 `lyre/task-{task_id}/{slug}`；push 同内容 commit 是 no-op | Git 天然 |
| Open PR（GitHub `gh pr create` / GitLab `glab mr create` / 自定义）| Agent 在 open 前 query 是否存在 `[task:{task_id}]` 标记的 PR；存在则跳过/更新 | Agent prompt 约束 |
| 写 artifact blob | 内容哈希命名（`sha256:abc...`）；重复写覆盖（相同内容）或 no-op | Lyre artifact 工具 |

**Agent prompt 应明确**：所有 Tier 1+ 操作执行前**必先 query 远端状态**。这是 persona spec 的强制条款。

### 5.2 Lyre 持久层副作用（由 outbox + dispatcher 负责幂等）

| 副作用 | 幂等机制 | 责任方 |
|---|---|---|
| Mailbox 消息发送 | 每条消息 `external_id = {wakeup_id}:{outbox_seq}`；接收方 mailbox 表 UNIQUE(external_id) ON CONFLICT DO NOTHING | Dispatcher + 接收方 mailbox 表 |
| Tier 1 通知派生 | `external_id = {task_id}:{side_effect_kind}:{side_effect_hash}`；同副作用只派生一次通知 | Dispatcher |
| Agent 内 `report_progress` | checkpoint 是覆盖语义；重做最新一次胜出 | Persistent layer |
| Lease 获取 | `UPDATE tasks SET lease_until = now() + lease_duration WHERE task_id = ? AND lease_until < now()`——并发安全 | Persistent layer |

**通用原则**：任何外部副作用都要有**自然键**或**人造 ID**用于去重。不可有"无 ID 副作用"。

---

## 6. Mailbox 在事务中的位置

### 6.1 写 mailbox（agent → 任意 actor）

`mailbox_send` 调用语义：

- 立即把 envelope append 到本次唤醒的 in-memory outbox buffer
- 返回 `MessageRef`（含未来的 external_id），agent 可继续工作
- 在 Step 9 COMMIT POINT 时，buffer 中每条 envelope 写入 outbox 表
- Outbox dispatcher 异步投递到接收方 mailbox

### 6.2 读 mailbox（agent 读自己的）

- 0005 之前：cursor 模型，`mailbox_read` 拉 `id > last_processed_msg_id` 的消息，
  `mark_processed` 把"待推进的偏移"记到 wakeup buffer，COMMIT POINT 时持久化。
- **0005 之后**：per-message read state，每条 `mailbox_messages` 有自己的
  `read_at TEXT`（NULL = unread）。`mailbox_read` 工具内**立即写** `read_at = now()`
  ——不等 COMMIT POINT。这意味着：
  - 同一封邮件再读不会被重发（已经标 read）
  - wakeup 中途崩溃也不会"丢失"读状态——已经持久了
  - 不需要 commit point 推 watermark；mailbox-side commit 跟 task checkpoint 解耦
- `mark_read` 工具给显式 dismiss FYI mail 用；同样不等 COMMIT POINT，立即写。
- Scheduler 的 Phase 0 auto-wake 防重发仍走一个独立的 `last_auto_triggered_msg_id`
  游标（写在 `mailboxes.metadata` JSON 里），跟 agent 的 read state 完全独立。

### 6.3 消费方幂等

接收方 mailbox 表：

```sql
CREATE TABLE mailbox_messages (
  id              BIGSERIAL PRIMARY KEY,
  recipient       TEXT NOT NULL,           -- actor_id
  external_id     TEXT NOT NULL,           -- 发送方 outbox 的 external_id
  sender          TEXT NOT NULL,
  urgency         TEXT NOT NULL,
  body            TEXT NOT NULL,
  task_id         UUID,
  parent_msg_id   BIGINT,
  metadata        JSONB,
  delivered_at    TIMESTAMPTZ NOT NULL,
  UNIQUE (recipient, external_id)
);
```

Dispatcher 投递时 `INSERT ... ON CONFLICT (recipient, external_id) DO NOTHING`——重复投递无副作用。

---

## 7. 任务续做协议

新 agent 接手一个未完成任务的精确流程：

```
[1] 调度器找到 lease 过期的任务
    SELECT * FROM tasks 
    WHERE status = 'in_progress' AND lease_until < now()
[2] 调度器派一个新 agent 进程（fork 新 subprocess + 新 tmpdir + 重新 git clone）
    输入 = AgentInput { task_id, persona, task, checkpoint = tasks.checkpoint, ... }
[3] 新 agent 启动 → Step 2 取 lease (UPDATE lease_until = now() + task.lease_duration)
    若 UPDATE 影响 0 行（其它 agent 抢先），放弃
[4] 加载 checkpoint
    checkpoint 含：
      - 上次提交完成的步骤 ID（"我做到哪了"）
      - 已 emit 消息 IDs（仅供 agent 校验幂等，正常情况下不需要重发）
      - 已 self_reported 的副作用列表（重启后 agent 用这个清单 + 远端实际状态对账）
      - 本次任务关键中间产物的指针（如生成的代码草稿在 local-hot 的指针）
[5] 装配 context（同首次唤醒，但 task_state 包含 checkpoint 信息）
[6] 从 checkpoint 标记的下一步开始执行
    若 checkpoint 显示曾自报过 Tier 1 副作用（已 push 分支、已开 PR），agent 必须先
    query 远端状态对账，避免重复操作
[7] 完成本次唤醒的提交 (Step 9)
```

**关键不变量**：agent 永不假设 tmpdir 内 / subprocess in-memory 任何状态在重启后保留。所有需要跨重启的状态都在 local-hot / global 持久层。

### 7.1 Checkpoint 字段草案

```ts
WakeupCheckpoint {
  task_id:               ID
  last_completed_step:   string                // 自由文本，如 "patch_drafted" / "tests_passed"
  state_machine_phase:   string                // 任务级状态机的阶段
  scratch_pointers:      { [key: string]: ArtifactRef }  // 中间产物在 local-hot 的指针
  emitted_message_ids:   ID[]                  // 已 emit 的消息 IDs（去重参考）
  self_reported_side_effects: SideEffectRecord[]  // subprocess 内 / 远端已发生的副作用记录
  schema_version:        int                   // checkpoint 自身的版本，便于升级
}

SideEffectRecord {
  kind:        "pushed_branch" | "opened_pr" | "created_issue" | ...
  details:     Record<string, any>             // 如 { branch: "...", commit_sha: "..." } 或 { pr_url: "..." }
  reported_at: time
}
```

---

## 8. 失败模式目录

| 失败类型 | 检测 | 恢复策略 |
|---|---|---|
| Agent 进程死亡（任意时刻） | Lease 超时（lease_duration 未刷新） | 调度器派新 agent，新 subprocess + 新 tmpdir，从 checkpoint 续做 |
| Subprocess 内 shell 命令失败（如 git push 网络问题） | Agent 在 subprocess 内观察 exit code | Agent 决定重试或换策略；多次失败可写 `report_progress` 记下，下次唤醒由新 agent 决策 |
| Lyre 工具调用失败（gateway 错误）| 工具返回 error | Agent 决定重试或换策略 |
| Step 9 跨存储 commit 失败（DB 错误） | DB 事务回滚 | 本次唤醒等于没做；调度器重派 |
| Outbox dispatcher 死亡 | 进程管理器心跳 | 重启 dispatcher；它从 outbox 未投递条目续做（幂等） |
| Outbox 投递永久失败（如 leader mailbox 不存在）| `dispatch_attempts > N` | Dispatcher 标记 row 为 `permanent_failure`，写 owner mailbox `urgency=blocker` |
| Postgres 不可用 | 工具 / DB 报错 | 整个 Lyre 暂停（这是基础设施级故障）；恢复后调度器自动 resume |
| 同一任务连续失败 N=3 次 | 调度器计数 | 写 leader / owner mailbox `urgency=blocker` 请示（Stop trigger） |
| Lease 误抢（split brain）| `UPDATE ... WHERE lease_until < now()` 的乐观锁失败 | 新 agent 自动退让；旧 agent 继续 |
| Container 内 Tier 1 副作用半成功（push 成功、open PR 失败）| Agent 自报中部分缺失 | 重启 agent 时根据 self_reported 与远端实际对账，补齐缺失副作用 |

---

## 9. 已识别但待解决的问题

> 起草 / 修订过程中浮现的子问题。

1. **Lease 续约机制**：v0.3 用 per-task 可配 lease_duration，避免长任务超时。是否还需要 agent 主动 `lease_renew()` 工具作为兜底？v0.3 不加，等实战碰到再说
2. ~~**Container 物理实现**~~ → 取消（v0.3 不用 per-agent 容器；agent 是 Lyre 派生的 subprocess + per-task tmpdir；OS 级隔离作为可选部署拓扑由 Lyre 整体容器化提供，详 [`AGENT_CONTRACT.md §4.2`](./AGENT_CONTRACT.md#42-可选lyre-整体容器化部署拓扑)）
3. **Outbox 投递顺序保证**：多消息之间是否需要保序？v0.3 不保证全局顺序，但同 `task_id` 内按 `outbox.id` 序投递
4. **Artifact blob 大小阈值**：超过多大走 object store？v0.3 暂定 1 MB
5. **Checkpoint schema 版本升级**：未来 schema 变化时如何兼容历史 checkpoint？v0.3 留 `schema_version` 字段，升级机制后议
6. **Dispatcher 的运行模式**：与 leader-persona agent 同处还是独立部署？v0.3 倾向独立——dispatcher 是基础设施，不该和 LLM 共生死
7. **跨任务的事务依赖**：A 任务的 outbox 行依赖 B 任务先 commit——v0.3 不支持此模式，所有任务事务独立
8. **Subprocess 内 Tier 1 副作用的对账逻辑**：v0.3 §3 kill 点 3 与 §7 描述了对账原则，但具体 query 协议（"agent 怎么知道远端 PR 是否存在"）需要 hosting-specific 实现，按 [`AGENT_CONTRACT.md §10`](./AGENT_CONTRACT.md#10-git-hosting-与-hosting-specific-行为) 走 persona 注入
9. **Webhook 接入（未来）**：当用户接入有 webhook 的 hosting 时，dispatcher 是否要接收 webhook 进行被动确认？或 webhook 直接落 outbox？v0.3 不做
10. **Tmpdir cleanup 边界情形**：subprocess 在 Step 11 之前异常退出后，tmpdir 的清理由 cleanup loop 兜底；扫描周期与孤儿 tmpdir 识别规则待定

---

