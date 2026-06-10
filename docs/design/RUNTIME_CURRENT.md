# RUNTIME_CURRENT — 运行时现状（as-built）

> status: **living — 随实现更新**
> changelog 规约：每次行为变更随 PR 在文末 Changelog **追加一行**（日期 + PR + 一句话），正文直接改写——本文档做 compaction，不做 append-only 堆积。
>
> **English TL;DR**: This is THE living description of the Lyre runtime as actually
> built (post-E0, 2026-06). Where the foundation-era docs (FOUNDATION /
> AGENT_CONTRACT / TRANSACTION_BOUNDARIES / AGENT_RUNTIME §2-4) conflict with this
> document, **this document wins** — they describe a v0.x architecture that was
> never fully built. A new contributor should read this file first, then dip into
> the round docs only for the *why* of individual decisions. Key as-built facts:
> wakeups start with an atomic wakeup-row+lease transaction; every effectful tool
> call is individually durable + idempotent at tool time (NOT buffered to a single
> Step-9 commit); inter-wakeup mail is at-least-once; the scheduler phase ladder is
> -1 / 0.5 / 0 / 0.7(dormant) / 0.8 / 2 / 3 / 4; personas MUST declare a
> model_preference (the documented LYRE_DEFAULT_MODEL fallback does not exist).

---

## 0. 本文档与其他文档的关系

| 想知道 | 去读 |
|---|---|
| **今天系统怎么跑**（本文档） | RUNTIME_CURRENT.md |
| 为什么是 mailbox-only / kill-test / 三档持久层 | FOUNDATION.md（理念仍有效；§3.3 网关机制等架构细节已被取代，见各处横幅） |
| fan-in barrier 的设计推演 | WORKFLOW_ORCHESTRATION.md |
| thread / 有界自邮件循环的设计推演 | AGENT_THREADS.md |
| 压缩 / 租约 / 死循环 / 取消等鲁棒性轮次的 *why* | LONG_RUNNING_ROBUSTNESS{,_2,_3}.md、FAILURE_ROBUSTNESS.md、ORCHESTRATION_ROBUSTNESS.md |
| 记忆组织 / skills 固化流 / 外包编码 | MEMORY_ORGANIZATION.md、BUILTIN_SKILLS.md、CAPABILITY_DISCOVERY.md |
| 已知 bug / 偏离全表 | DEEP_REVIEW_2026-06.md（本文档 §7 只给索引，不复述） |

---

## 1. 一次 wakeup 的真实生命周期

入口：`Scheduler._run_task_inline`（`src/lyre/scheduler/scheduler.py`）。
`lyre serve` 默认 **subprocess 模式**（`--subprocess/--no-subprocess`，默认 on）：每个
task spawn 一个 `lyre run-task <task_id>` 子进程（`main.py` 中 hidden 命令），子进程内
仍然执行同一个 `_run_task_inline`；并发上限 `max_concurrent_tasks`（默认 4），inline
模式严格串行。Agent 是顺序 actor：同一 agent 不允许两个并发 wakeup（Phase 3 的
`has_active_for_agent` 门 + 当 tick 的 `claimed_in_this_tick` 集合；subprocess 模式下
这个门有一个已知跨 tick 窗口，见 §7 [7]）。

按时间顺序：

1. **孤儿清扫** — `close_orphans_for_task(task_id)`：上一次尝试可能在写入 wakeup 行后
   死掉，先关掉本 task 名下所有未结束的 wakeup 行（按 task_id 收窄，不碰同 agent 的
   其他 task）。
2. **原子 start+claim**（E0 修复，scheduler.py ~1560）— `wakeups.start`（INSERT 行）与
   `tasks.claim_lease`（乐观 UPDATE WHERE 过期/无租约）在**同一个
   `repos.transaction()`** 里提交。抢租约失败抛 `_LeaseUnclaimed` 把 INSERT 一并回滚
   ——输掉竞争不留任何痕迹。两次提交时代的"中间被 SIGKILL → agent 永久砖死"窗口
   （DEEP_REVIEW C-1）已不存在：要么什么都没发生（下个 tick 重试），要么行+租约
   同时存在（之后崩溃走普通租约恢复）。
3. **worktree + git 供给** — 每个 wakeup 都有一个 worktree（空 tmpdir，
   `object_store/worktrees/` 下）；task 带 `git_context` 时再叠加 ephemeral SSH key +
   ssh-agent + clone/checkout。git 供给失败：释放租约、task 标 failed（已知缺口：不发
   task_terminated 邮件且 wakeup 行未关，见 §7 [11]）。
4. **MailWatcher**（`runtime/mail_watcher.py`）— 后台 asyncio 任务以 1s 轮询本 agent
   收件箱中 baseline（wakeup 开始时最大 msg id）之后的新邮件：
   - `urgency=blocker` → **流中打断**（在 LLM stream 事件之间 break 出当前 stream）；
   - `urgency=high` → **turn 边界注入**一条 user-role 通知。
   存量未读邮件不走 watcher——它们经 Phase 0 的"check inbox"任务进初始上下文。
5. **AgentLoop.run()**（`runtime/agent_loop.py`）— 见下。
6. **Step-9 fenced finalize** — 见下。
7. **best-effort auto-summary sidecar** — 见下。
8. **finally 清理** — 释放租约、关 transcript fd、git teardown、worktree 清理
   （成功才删目录，失败留尸检现场）。chaos 模拟 kill 时跳过整段 finally，
   逼真复现"进程死了没有 finally"。

### 1.1 AgentLoop 逐 turn

System prompt 由 `runtime/context.py` 按 stable→volatile 组装（prompt-cache 友好）：
字节级稳定的 identity preamble（mailbox 协议、ack-and-stop / phantom-delegation 诫令、
scratchpad 例程、flat-id 记忆路径）→ persona 正文 → `APPEND.md` → `SYSTEM.md` →
`user.md` → AGENTS.md 向上行走 → 记忆索引（`## Available global memory`）→ skills
XML（只有 name+description，正文按需经 `read_memory` 加载）。初始 user 消息**推送**
（不是让模型拉取）goal/acceptance/checkpoint、子任务、scratchpad 内容、近期已发邮件
头、thread 历史。

每个 turn（上限 `max_turns`，默认 24；dispatch 可经 `dispatch_task(max_turns=)` 写入
`tier_overrides` 按 task 提升——O3a）：

- **turn 边界检查**：B2 操作员取消（`cancel_check` 读 task metadata 的持久 cancel
  flag）；MailWatcher 信号 → 注入新邮件通知。
- **逐候选 fallback**（`_run_one_turn_with_fallback`）：按 router 排好的 candidates
  依次尝试；跳过熔断开路的；对无 vision 能力的候选剥离图像块；流开始前出错 →
  下一候选；**流中**出错 → 有界 failover（`max_midstream_retries`，默认 1），半截输出
  丢弃——安全，因为工具只在 turn 完整返回后才 dispatch。
- **结束条件**：某个 turn **没有任何 `tool_use` block** → wakeup 结束。`stop_reason`
  与 tool_use 并存时只是元数据不是控制信号（DeepSeek 会在 tool_use 旁发
  `end_turn`）；没有也永远不会有 `end_turn` 工具。`mailbox_send` 不结束 wakeup。
- **工具 dispatch**：双重 allowlist（persona `allowed_lyre_tools` ∩ 注册表）；
  畸形参数走 `_raw` 守护；多模态结果块挂在 tool_result user 消息上。assistant 消息
  追加时 thinking block 必须在最前（Anthropic extended-thinking 绑定约束，见
  `compact.py` / AGENT_RUNTIME 对应节）。
- **silent-turn nudge**：本 turn 只用了信息收集类工具（不在 `_USER_FACING_TOOLS`
  集合内）且还没做过用户可见动作 → 注入催促，预算 2 次。
- **H1 死循环闸**：对本 turn 工具调用做 (name, args) 指纹；与上 turn 完全相同连续
  达到 `loop_repeat_threshold`（config 默认 5，0 关闭）→ 先 nudge 一次，仍重复 →
  经 S0 缝合作式停止（`needs_continuation`）。
- **自动压缩**：本 turn `input_tokens ≥ compact_threshold(默认 0.7) × context_window`
  → `compact.py` 原地重写 messages：pivot 锚定倒数第 K 个 assistant 消息（K=3，从
  构造上保证 tool_use/result 对不被切断）；保留 messages[0]（任务目标）+ 尾部；被
  略去范围内 `mailbox_get_message` 结果变合成 user 消息、`mailbox_send` 输入变合成
  assistant 消息（**邮件逐字保留**，铁律五），列表类/幂等工具直接丢，其余经一次
  **同模型** LLM 摘要（失败 → 原始 trace 降级并打 `summary_degraded` 标）；产物标
  `compaction_artifact`，重压缩时逐字携带（幂等）。adapter 不报 Usage 时按 chars/4
  估算（D1），压缩与峰值指标不致失明。**≥3 次压缩仍超限 → thrash bail**：强制走
  silent_close 收尾（已知问题：道歉信可能失实且 task 记 completed，见 §7 [5]）。
- **A1 租约心跳**（scheduler 侧并行任务）：每 `lease_duration_s/3` 续租，
  `WHERE lease_holder=?` 自我 fencing；租约被夺或超 `wakeup_wall_budget_s`
  （默认 0=关）→ 经 S0 请求停止。

### 1.2 结果状态判定（优先级严格）

```
S0 stop_request（B2 取消 / wall budget / 租约丢失 / H1 死循环 → 各自 target_status）
  > hit_max_turns（for...else 精确捕捉跑满截断 → needs_continuation，绝不算 completed —— A2）
    > silent_close（耗尽 nudge 预算且无用户可见动作 → 向被自动标读邮件的 asker 发
      幂等道歉信）
      > completed（当且仅当最终 stop_reason == end_turn）
```

scheduler 把 wakeup 状态映射到 task 状态：`silent_close → completed`（沉默细节留在
wakeups.end_status 上）、`needs_continuation → failed`（诚实截断，可重派）。

### 1.3 Step-9 fenced finalize（一个事务）

`repos.transaction()` 内一次提交三件事（成功路径与异常路径**镜像**同一结构）：

1. `wakeups.end`（end_status + 全部计量：tokens、wall、工具数、压缩次数、上下文峰值）；
2. `tasks.update_status(..., holder_wakeup_id=wakeup_id)` —— **fenced**：只有仍持有
   租约才能推进状态，被夺租的旧 worker 写不进终态；
3. fenced 写成功（still_holder）才入队 `task_terminated` outbox 邮件
   （external_id=`task_terminated:<task_id>`，首写胜）。

finalize 前还有 O2 检查：fan-in leg 以 completed 收尾但从未提交 typed result →
task 降级 failed（`fan_in_no_typed_result`），让 O1 哨兵把死 leg 暴露给协调者。
`task_terminated` 邮件对 fan-in leg、ephemeral agent（reaper 自己管生死）、成功的
auto-inbox 任务、成功的顶层任务（agent 自己已回信）**抑制**；失败一律上报。

### 1.4 auto-summary sidecar（finalize 之后，best-effort）

`runtime/wakeup_summary.py`：一次 cheap-tier 模型调用（无 cheap 模型则整体跳过，
绝不抛错），把本 wakeup 压成几条 bullet，**最新在前**追加到
`memory/facts/agent-<flat-id>-notes.md` 的 `## Auto-summary log` 节下。路径经
`identity.agent_notes_rel_path`（E0 把 flatten 收编为 SSOT，spawned `persona/name`
agent 不再写进无人读的子目录）；写入走 `fsutil.atomic_write_text`
（mkstemp+fsync+replace，E0 新建的全库唯一原子写原语）。超过 `notes_max_entries`
（默认 0=不轮转）时 kill-safe 轮转：先 archive-append-fsync 到
`object_store/notes_archive/agent-<flat-id>.md`，再原子重写，按 wakeup-id 去重。

---

## 2. 事务模型（as-built —— 取代 TRANSACTION_BOUNDARIES 的 Step-9 叙事）

> TRANSACTION_BOUNDARIES.md 的核心定理（"全部写入缓冲到 Step 9 单一原子提交点，
> Step 1-8 任意 kill = 本次唤醒没发生过"）**不再描述本系统**。现实模型是：

**每个效果性写入即时持久 + 确定性 external_id 幂等 + 末尾 fenced finalize。**

- `lyre serve` 下 scheduler / outbox dispatcher / channels / dashboard 共享**一条**
  aiosqlite 连接（WAL、foreign_keys ON、busy_timeout 10s）。DAO 的每个 mutator 以
  `_commit` 收尾；`repos.transaction()` 块内 `_commit` 退化为 no-op（ContextVar），
  块本身持 per-connection commit 锁覆盖整个 BEGIN..COMMIT/ROLLBACK（已知缺口：锁只
  守 commit 边界不守语句执行，见 §7 [13/16/54]）。
- `mailbox_send` 在**工具调用当时**就把 outbox 行落库提交（不是缓冲到 wakeup 结
  尾）。external_id = `{wakeup_id}:{tool_use_id}:{recipient}`，逐收件人；outbox 表
  UNIQUE(kind, external_id)，投递端 `insert_message` UNIQUE(recipient, external_id)
  ON CONFLICT DO NOTHING。同一 wakeup 内重试零成本去重。
- 邮件已读状态逐消息即时写；checkpoint 经 `report_progress` fenced 即时写并回注
  下一次 wakeup 的初始消息；scratchpad / notes / persona 文件全部原子写（fsutil）。
- **跨 wakeup 重跑的邮件语义是 at-least-once**：租约过期重跑产生新 wakeup_id，
  external_id 随之不同——崩溃前已发出的邮件**留存**，重跑会再发一遍。重复在社交层
  吸收（收件人看到两封同义邮件），不在机制层去重。这是接受的取舍，不是 bug。

各 kill 窗口留下什么：

| SIGKILL 落点 | 留下 | 恢复 |
|---|---|---|
| start+claim 事务前/中 | 无任何痕迹（事务原子） | 下个 tick Phase 3 照常认领 |
| wakeup 进行中 | 已发邮件（持久）、已读标记、checkpoint、open wakeup 行 + 持有中的租约 | 租约到期 → Phase 2 重跑（邮件 at-least-once 重发） |
| Step-9 事务中 | 事务原子：要么三件全落、要么等同"进行中" | 同上；task_terminated 与终态同生死，不会出现"终态无邮件" |
| finalize 后、finally 清理前 | task 已终态；租约/worktree 残留 | 租约对终态 task 无害（`find_expired_leases` 只看 in_progress）；启动审计记录孤儿 wakeup 行 |

---

## 3. 调度器相位表

`Scheduler._tick`（默认 1s 轮询）每 tick 顺序执行。**编号是堆积史的化石**：各轮次把
新相位按"必须跑在谁前面"插进既有序列（-1 要在 0 前，0.5 要在 0 前……），而原
Phase 1（认领 + parent-resume）被 fan-in 邮件驱动设计取代后删除，空号保留——保号
不重编是有意的，让历轮文档/日志的相位引用不失效。

| 相位 | 一句话 |
|---|---|
| **-1** 未来邮件 | 把到期 `scheduled_mail` 投成真实邮箱消息（external_id=`sched:<id>:<occurrence>` 幂等）；T4 循环预算：达到 `max_occurrences` 时末次投递升 high 注明"最后一次"并停止续期 |
| **0.5** fan-in barrier | 数**已投递**的 result 邮件（distinct leg_key）；先做 O1 对账（给终态失败且无结果、无 pending outbox 的 leg 插幂等哨兵 `fanin:<g>:<leg>:failed`）；quorum/deadline/TTL 满足 → **先插协调者 ready 邮件、后做单赢家 guarded 状态翻转**（kill 在中间 = 重投而非丢失） |
| **0** 邮件自动唤醒 | 任何非 owner、无在途任务、有未读 urgency≥normal 邮件的 agent → 自动派一个 "check inbox" task；`last_auto_triggered` 游标 + 批量 busy-set 防自激 |
| **0.7** park/resume | **休眠**——把 `needs_input` 且 resume 标志已立的任务翻回 pending。今天没有任何东西 park 任务，`find_resumable()` 恒空；保留为未来 barrier 的单写者缝 |
| **0.8** reaper | ephemeral agent 监督回收：OTP 重启策略 `temporary`(默认,不重启)/`transient`(失败才重启)/`permanent`(总重启)，滑动强度窗口默认 max_restarts=3 / max_seconds=60s；超限 → 幂等升级邮件给 spawner-或-owner 后回收 |
| **2** 租约恢复 | 重跑租约过期的 in_progress 任务（进程死亡/SIGKILL）；ephemeral 与 bootstrap singleton 的反复失败经 `bump_recovery_attempt`/强度预算封顶并升级，不无限重跑 |
| **3** 认领新工作 | 领 pending 任务：bootstrap singleton 优先 + 预留一个 slot（子代占不满全部并发）；`has_active_for_agent`（open wakeup 行 JOIN 非终态 task）保证每 agent 串行；候选超采 ×4 |
| **4** 维护（C4） | 节流的 DB 维护（默认至多 6h 一次，仅 `retention_days>0` 时）：终态 outbox/wakeups/scheduled_mail/fan_in 裁剪 + WAL checkpoint；绝不碰 mailbox_messages/blobs/artifacts |

异常处理粒度：`run()` 只在 **tick 级**捕获。Phase -1/0/0.5 没有逐行隔离——一个毒行
可使每 tick 在任务派发前夭折（见 §7 [12]）。

---

## 4. 编排原语

全部由**持久邮件行**组合而成，没有任何同步原语、没有进程内回调。

### 4.1 dispatch_task（`runtime/tools/tasks.py`）

子 task 写入与（可选的）fan_in 成员登记在一个事务里。spec 携带：goal/acceptance、
`lease_duration_s`（默认 1800）、`git_context`、`max_turns`（落 `tier_overrides`，
O3a）、metadata；`thread_id` 从派发者的 ToolContext **机械继承**（T2）。dispatch 之后
协调者**不等待**——要么继续干别的，要么停止调用工具结束 wakeup；子任务的终态经
task_terminated / fan-in 邮件 + Phase 0 自动唤醒送回来。这是被删除的
parent-park/parent-resume 模型的替代品（WORKFLOW_ORCHESTRATION.md）。

### 4.2 fan-in barrier（工具 `fan_in_open/status/results/cancel`）

协调者 `fan_in_open`（expect_replies、quorum、**result_schema 必填**、deadline_in_s）
→ 逐 leg dispatch → leg 以 `mailbox_send(result_for={group_id, leg_key})` 提交 typed
result（lineage + JSON-schema 校验在 `tools/mailbox.py`）→ Phase 0.5 数已投递结果，达成即
mail-before-flip 唤醒协调者 → 协调者 `fan_in_results` 取全部结果（O1 哨兵被拆进
`failed_legs`）。失败可见性三件套：O1 哨兵（leg 死了也计数）、O2 降级（leg 没交
结果不算 completed）、reaper 升级邮件。已知组合缺口（晚到结果 × 重启策略）见
§7 [8][9][10]。

### 4.3 监督与 reaper

ephemeral agent 在 `metadata.supervision` 里声明 `ephemeral: true` + 重启策略；
Phase 0.8 按 §3 表回收/重启/升级。Phase 2 对反复崩溃（从不到终态、reaper 看不见）
的 ephemeral 复用同一强度预算。升级路径：spawner（活着且未归档）否则 owner，
幂等 external_id（`supervision:<agent>:escalation`）。

### 4.4 threads（AGENT_THREADS.md，T1-T4 全部已落地）

- **T1** 推送式上下文：scratchpad 内容 + 近期已发邮件头注入初始消息；
- **T2** `thread_id` 铸造 + reply/dispatch/result 机械继承（agent 不用维护）；
- **T3** 带 thread 的 wakeup 自动注入本主线邮件历史；
- **T4** 有界自邮件循环：`mailbox_send` 的 `deliver_at/deliver_in/recur_*` 参数把邮件
  写进 `scheduled_mail` 表（即时自发被禁止——投递延迟是防 runaway 的扣环），
  `max_occurrences` 预算由 Phase -1 强制（scheduler 而非模型执行上限）：末次投递升
  high 注明最后一次并停止续期。设计稿里的 token 预算**未实现**（`budget_tokens` 列
  reserved）。`list_scheduled_mail`/`cancel_scheduled_mail` 管理（已知所有权缺口
  §7 [26]）。跨 wakeup 无进展闸 H2 设计已定稿但**尚未实现**（§7 H2 行，DEEP_REVIEW §F1）。

---

## 5. 记忆面

| 层 | 位置 | 写者 | 说明 |
|---|---|---|---|
| checkpoint（local-hot） | `tasks.checkpoint`（DB JSON） | agent 经 `report_progress`（fenced） | task 结束即弃；回注下一 wakeup 初始消息 |
| scratchpad | `~/.lyre/memory/scratchpad/<flat-id>.md` | agent 经 `update_scratchpad`（`append` 默认 / `overwrite` 整理） | 短期工作记忆；identity preamble 教 agent 醒来先读；原子写（E0） |
| notes | `~/.lyre/memory/facts/agent-<flat-id>-notes.md` | agent（写 facts 区）+ 运行时（追加 `## Auto-summary log`） | 长期记忆；flat-id（`/`→`-`）是 `identity.flat_id`/`agent_notes_rel_path` 的 SSOT；轮转见 §1.4 |
| facts / 全局记忆 | `~/.lyre/memory/` 其余 markdown | owner / agent | `runtime/memory.py` 扫 frontmatter description 生成 system prompt 里的 `## Available global memory` 索引；正文按需 `read_memory` |
| skills | `~/.lyre/skills/{approved,proposed,archived}/<name>/SKILL.md`（**目录形**，flat 文件不被识别）+ 内置库 `src/lyre/data/skills/`（运行时直读，不拷贝） | 固化流：worker 写 `proposed/`，reviewer 审后 FS mv 到 `approved/`（CAPABILITY_DISCOVERY / BUILTIN_SKILLS） | prompt 注入收叠的 name+description；同名 first-wins，用户 approved 覆盖 builtin；加载诊断有算无报（DEEP_REVIEW [30]） |
| transcripts（冷档） | `object_store/wakeups/<wakeup_id>/transcript.jsonl` | 运行时（TranscriptWriter，append-only，close 时 fsync） | **运行时只写不读**；仅 dashboard/CLI 观测用 |
| blobs | `object_store/blobs/`（sha256 内容寻址，原子写幂等） | 运行时 | `blobs` 表只存元数据 |

---

## 6. 模型路由

```
registry → persona.model_preference（必填）→ router 排名 → 逐 turn fallback → 熔断器
```

1. **registry**：打包的 `src/lyre/data/model_registry.yaml` + 用户 `config.toml`
   `[[models]]`。语义是**整体替换**：用户写了任何一条，shipped 条目全部失效
   （不是按 id 合并——docs/configuration.md 旧说法已废）。
2. **persona model_preference 是必填项**：`tier`（flagship/workhorse/cheap）+
   `requires`（能力标签子集匹配）+ `prefer`（按序点名 model id）。缺失 →
   `Scheduler._preference_for` 直接 RuntimeError 炸掉该 wakeup。
   **文档曾承诺的 LYRE_DEFAULT_MODEL fallback 不存在**（§7 [57]）。
   `LYRE_MODEL_OVERRIDE` 压倒一切（测试用）。
3. **router 排名**（`runtime/model_router.py`）：先按 `requires` 能力过滤
   （不满足 → 全空时抛 NoEligibleModel），再排序：`prefer` 中的索引最优先
   （点名的模型赢过仅 tier 匹配的），其次 tier 匹配，输出有序候选列表。
4. **逐 turn fallback**（§1.1）：每个 turn 重新沿候选列表走，跳过熔断开路者；
   健康度不影响排名只影响可用性。
5. **熔断器**（`runtime/health_tracker.py`）：per model_id，60s 滑窗内 3 次失败开路；
   180s 冷却后 half_open 放行一次探测；探测成功闭合，**探测失败立即重开一轮冷却**
   （E0 修复——此前首轮冷却后断路器永久失效）。
6. **adapter 工厂**（`runtime/adapter_factory.py`）：provider→class 分支目前硬编码在
   adapter/ 之外（加 provider 实际是"一个 adapter 模块 + 一条 registry + 一个工厂分
   支"）。E0 后 Anthropic adapter 透传真实 stop_reason（`_STOP_REASON_MAP`，含
   max_tokens），A2 截断分类在默认 provider 上恢复有效。

旁路 LLM 调用（compact 摘要、wakeup auto-summary）同样走 LLMAdapter 接口——
铁律一在代码层双向成立。

---

## 7. 已知偏离与在修项

只给索引；陈述、证据与修法见 `DEEP_REVIEW_2026-06.md`（编号即该文 findings 编号）。
E0（C-1 原子 start+claim、C-2 原子 scratchpad 写、[20] stop_reason、[21] 熔断重开、
[4]/[27] notes flatten）已落地，本文档正文即 post-E0 现状。

| 领域 | 编号 | 现状 |
|---|---|---|
| subprocess 双 wakeup 窗口 / Phase 2 缺 per-agent 门 | [7] | 待修（F3） |
| fan-in 晚到结果 × O2 × transient 重启的烧钱循环；重启非 one-for-one；哨兵/重启时序 | [8][9][10] | 待修（F1/F3） |
| 三条失败路径不发 task_terminated（git 供给失败还留 open wakeup 行） | [11] | 待修（F3） |
| Phase -1/0/0.5 无逐行错误隔离 | [12] | 待修（F2） |
| 共享连接 transaction() 回滚可吞并发写 | [13][16][54] | 待修（F3，dispatcher 独立连接方向） |
| `_row_to_task` 等读回丢列 | [14] | 待修 |
| 压缩把失败的 mailbox_send 记成已发；thrash-bail 假道歉 + 截断记 completed | [3][5] | 待修（F6） |
| thinking 块跨模型 fallback 不剥离；Anthropic 从不启用 extended thinking；Responses reasoning 回放缺失 | [6][24][23] | 待修（F5） |
| mailbox_read 他人收件箱标读；cancel_scheduled_mail 无所有权 | [25][26] | 待修（F4） |
| H2 跨 wakeup 无进展闸：设计定稿、零实现 | DEEP_REVIEW §F1 | 待做 |
| 死物：mcp_server 空壳、skills/artifacts/local_hot 表+仓、StreamError 等 | [19][52]，DEEP_REVIEW §E2 | 待删 |
| Phase 0.7 park/resume 休眠 | — | 保留的设计缝，非缺陷 |

---

## Changelog

- 2026-06-10 — 初版（E1 正典对齐批次）：吸收 6 份 round-doc 结论 + DEEP_REVIEW 子系统地图，按 post-E0 实现写就。
