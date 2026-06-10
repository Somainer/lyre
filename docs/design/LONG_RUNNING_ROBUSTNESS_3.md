# Lyre — 长跑健壮性(三):收口无界增长与空转

> **文档定位**:接 `LONG_RUNNING_ROBUSTNESS_2.md`(LR2:恢复诚实性 + wakeup *内层*反失控 H1 + 手动取消 B2)。本轮收掉剩下三个"会一直涨或一直转"的与负载无关的口子——(D1) adapter 不吐 `Usage` 时压缩被**静默关闭**;(C4) DB 表**无界增长**、无 VACUUM/WAL checkpoint(RB-3 只收了文件侧 notes,DB 侧没动);(H2) 跨 wakeup 的预算自驱动 loop 只按**计数**停,预算内可**空转**("沿无意义路径继续")。全部纯 Python+SQL+FS,组合现有 primitive,不碰五铁律内核。
>
> **English one-liner**: Round three bounds what still grows or spins over a months/years horizon — a client-side token-estimate floor so a Usage-omitting adapter can't silently disable compaction (D1); DB retention + VACUUM/WAL maintenance to complement RB-3's filesystem-side notes rotation (C4); and a *no-progress* gate on the budgeted self-mail loop so it stops on lack of progress, not just on a count (H2) — finishing the anti-runaway story H1 started within a single wakeup.
>
> **相关**:[`LONG_RUNNING_ROBUSTNESS_2.md`](./LONG_RUNNING_ROBUSTNESS_2.md)(LR2:S0 seam / A1 / A2 / H1 / B2);[`LONG_RUNNING_ROBUSTNESS.md`](./LONG_RUNNING_ROBUSTNESS.md)(RB-3 notes 轮转,本轮 C4 是其 DB 侧对偶);[`AGENT_THREADS.md`](./AGENT_THREADS.md)(T2 thread_id / T4 有界自驱动,H2 直接扩展 T4 的 Phase -1 停-loop 路径);[`FOUNDATION.md`](./FOUNDATION.md) 铁律三/四/五。
>
> **owner 对齐**:2026-06-08,选定本轮做 **D1 + C4 + H2(3 PR)**,先文档后实现。沿用 MVP 约束:不设硬美元预算;反失控只靠 harness 强制停。
>
> **状态**:**D1+C4 已落地**(PR #49,commit `abeeba9`);**H2 设计已定稿、未实现**——零代码(`no_progress` 在 src/migrations/tests 全无命中,2026-06-10 验证),planned as **F1** in `DEEP_REVIEW_2026-06.md`。(§9 两处决策已拍:C4 自动+手动都给、H2 work-AND-no-output。)本轮叠在 LR2(PR #48)之上。PR 路线见 §6。

---

## 1. 背景:LR2 之后还剩什么

LR2 让恢复变诚实(A1/A2)、堵住了 wakeup **内层**死循环(H1)、给了 operator 手动取消(B2)。但"跑数月到数年"还有三个**与负载无关**的结构性口子没收:

1. **压缩可被 provider 漂移静默关闭**(D1):压缩触发只看 provider 发来的 `Usage.input_tokens`。新增/换 adapter 是铁律一的常态;一个不吐 `Usage` 的 OAI-compat 代理 / 没开 `include_usage` 的 vLLM → `turn_input=0` → 压缩守卫永不满足 → 直接撑爆上下文窗口。
2. **DB 单调增长**(C4):`wakeups` / `outbox` / `blobs` / `fan_in_*` 每条都是只增行,全仓库零 `DELETE`/`VACUUM`/WAL checkpoint 管理。RB-3 只把**文件侧** notes 下沉冷档,**DB 侧**无任何有界化——跑几年必然膨胀、查询变慢、WAL 涨。
3. **自驱动 loop 只按计数停**(H2):T4 给了 loop 的 `max_occurrences`/`recur_until`/token cap,但都是"还能转几圈",不管"转得有没有用"。预算内一个 loop 可以 re-arm 满 `max_occurrences` 次、零进展——正是 owner 最初的痛点"沿无意义路径继续",LR2 里被显式押后(H2)。

---

## 2. 问题(对照源码)

### D1 — adapter 不吐 Usage → 压缩静默失效
`turn_usage` 仅由 provider 的 `Usage` 事件填充(`agent_loop.py:445`/`458`),压缩守卫要求 `turn_usage[0]` 为真(`agent_loop.py` 自动压缩段:`ctx_window and turn_usage and turn_usage[0] and turn_usage[0] >= compact_threshold*ctx_window`)。三个 adapter 都**有条件**地发 `Usage`:`openai.py:231 if usage_payload:`、`anthropic.py:334 if usage is not None:`、`openai_responses.py` 同形。`docs/configuration.md` 把 OpenRouter / Together / vLLM 都路由进 openai adapter——其中任一不回 usage,该路由下**每个** agent 的压缩都被关掉,且 `context_peak_tokens` 恒 0(observability 也瞎了)。这与 `model_registry.py` 缺 `context_window` 配置那条(ctx 0%)是**不同**根因。

### C4 — DB 无保留 / 无 VACUUM / 无 WAL checkpoint
`db.py:12-17` 的 PRAGMA 只设 `journal_mode=WAL` + `synchronous=NORMAL` 等,**无** `wal_checkpoint`/`auto_vacuum`/`VACUUM` 策略。0001 的 17 张表里,只增行的有:`wakeups`(每次 wakeup 一行)、`outbox`(投递后 `mark_delivered` 标记但不删,`sqlite_impl.py:1827`)、`mailbox_messages`、`mail_reactions`、`blobs`/`artifacts`、`fan_in_groups`/`fan_in_members`、`scheduled_mail`(completed 后留存)。全仓库 grep 无 `DELETE FROM (wakeups|outbox|...)`、无 `VACUUM`。WAL 在长跑单进程下若不主动 checkpoint 会持续增长。

### H2 — 自驱动 loop 只按计数停
`_deliver_due_scheduled_mail`(`scheduler.py:982`)的 re-arm 决策只看 `loop_final = max_occurrences 到顶`(`:1035-1038`)与 `compute_next_fire`(`:1076`,`recur_until` 到点)。没有任何"这一轮有没有前进"的判定。`occurrence_count` 是纯计数器。进展信号现成但未被消费:`report_progress` 把 `task.checkpoint` 落库(`progress.py:29`),thread_id 在 mail/task metadata(T2),outward mail 是 `mailbox_messages` 行——都可按 thread 查。

---

## 3. D1:客户端 token 估计兜底(纯防御)

**保证每个 turn 都有非零的 input-token 信号**:当本 turn 没收到任何 `Usage` 事件时,回退用 `chars//4`(system_prompt + 已发 messages 的字符数 /4)粗估 `turn_usage[0]`。

- **真 Usage 永远优先**:合规 adapter 行为零变化;只有 `turn_usage[0]` 为空才用估计。
- **放 loop 而非 per-adapter**(实现时定):`_estimate_input_tokens` 在 `agent_loop` 消费 `turn_usage` 处兜底。这样**每个** adapter——包括未来某个忘了发 Usage 的新 adapter——都被**自动**覆盖,正好服务 D1 的初衷(provider churn 韧性);`chars/4` 不含任何 provider 知识,放 loop 不泄露 provider 细节(铁律一不破)。打**一次性** `usage_estimated_fallback` 告警 + transcript note 让 operator 看见非合规 adapter。
- 估计同时喂压缩守卫与 `context_peak_tokens`,使压缩与 observability 在 provider churn 下不失效。
- 离线测试:`compaction_fires_from_client_estimate_when_adapter_omits_usage` / 真 Usage 时不估计 / 估计只在缺 Usage 时出现。

**无 schema、无契约变更。**

## 4. C4:DB 保留 + VACUUM/WAL 维护

一个**低频维护动作**(默认关,opt-in,随大流的安全旋钮):

- **清理(按 `created_at`/`delivered_at` 超 `retention_days`)**:
  - `outbox`:已投递行(`delivered`/`sent`)超期删——它是发件中转,投妥即无用。
  - `wakeups`:超期的**指标行**删(审计密度在冷档 transcript,不在这张表);可保"每 agent 最近 K 行"兜底。
  - `scheduled_mail`:`completed`/`bounced` 且超期删。
  - `fan_in_groups`/`members`:已 resolve/过期且超期删。
  - `mail_reactions`:孤儿(指向已删 mail)清。
- **绝不删**(铁律五/四):`mailbox_messages`(owner/peer 逐字通讯)、`blobs`/`artifacts`(被 mail 引用的内容)、cold transcripts(本就在 FS 冷档,不在 DB)。
- **空间回收**:每次维护跑 `PRAGMA wal_checkpoint(TRUNCATE)`;周期性 `VACUUM`(或开 `PRAGMA auto_vacuum=INCREMENTAL` + `incremental_vacuum`)。
- **kill-safe**:删除是幂等的按时间窗 `DELETE`,中途 kill 下次维护续删,不丢不重(被删的都是已终态/已投递行)。
- 离线测试:超期行被清 / 未超期保留 / `mailbox_messages` 永不动 / 被引用 blob 不删 / checkpoint 后 WAL 缩 / 关(默认)时不动。

> **§9 决策①**:保留窗口取值、是否保"每 agent 最近 K 行 wakeups"、以及**触发方式**(调度器低频维护 phase vs 手动 `lyre maintenance` CLI vs 两者都给)。

## 5. H2:跨 wakeup 无进展闸(扩展 T4)

在 Phase -1 re-arm **前**,对 loop 所在 thread 算一个**进展信号**;连续 K 轮无进展 → 走 T4 **现成**的"停 re-arm + 发 final 高优邮件"路径(`scheduler.py:1041-1047`/`1082-1083`),把触发条件从 `loop_final(计满)` 扩成 `loop_final ∨ no_progress 到顶`。

- **状态**:`scheduled_mail` 加 `no_progress_count INTEGER`(镜像现有 `occurrence_count`)+ `max_no_progress INTEGER`(镜像 `max_occurrences`)。单基线就地编辑 0001,无新表。
- **进展信号(纯持久行,不靠模型自觉)**:自上次投递(`last_delivered_at` / 上一 occurrence)以来,该 thread 上**有没有外向产出**——任一为真即"有进展":① 出现了 thread_id 命中的**非自**`mailbox_message`;② loop 的 `task.checkpoint` 被更新(`report_progress`);③ task 状态推进。无进展则 `no_progress_count += 1`,有进展则归零。
- **harness 强制**:cap 在调度器 Phase -1 执行(与 T4 的 occurrence cap 同构),模型越不过自己声明的 `max_no_progress`。

> **§9 决策②**:进展口径——区分**等待型心跳**(本就该很轻、定期看一眼没事就睡,不该被误杀)与**空转型 thrasher**(烧了一堆 turn/token 却没产出)。
> - **(推荐)work-AND-no-output**:只有"这一轮触发的 wakeup 做了实质工作(turns/token 超地板)**且**无外向产出"才记一次空转 → 卡 thrasher、放过 waiter。代价:需 join `wakeups` 指标按 thread 统计本轮工作量。
> - (简版)pure-no-output:只看"连续 K 轮无外向产出",不看工作量。更简单,但会误杀长期等待型心跳。
> - (最简)checkpoint-only:只认 checkpoint 变化为进展。最省,但依赖 agent 调 `report_progress`。

不改 agent 契约(loop 仍是有界自邮件);不引入 H2 专用机制(复用 T4 的停-loop 路径)。

---

## 6. 分 PR 路线

| PR | 内容 | 关键离线测试 | 依赖 |
|---|---|---|---|
| **D1** | adapter seam:缺 Usage 时回退 chars/4 估计 + 一次性告警;喂压缩守卫与 context_peak | 缺 Usage→压缩仍触发 / 有 Usage→不估计 / 估计只在缺时出现 | 无(独立,先落) |
| **C4** | DB 保留(超期清 outbox/wakeups/scheduled_mail/fan_in,绝不动 mailbox)+ `wal_checkpoint(TRUNCATE)` + VACUUM;config `retention_days` 默认 0=关;触发(见决策①) | 超期清/未超期留/mailbox 不动/引用 blob 不删/WAL 缩/默认关不动 | 无(独立) |
| **H2** | `scheduled_mail.no_progress_count/max_no_progress` + Phase -1 进展闸 + 复用 T4 final 邮件 + `mailbox_send` 参数 | 无进展连续 K→停+final / 有进展归零 / 等待型心跳不误杀(按决策②) / 未设 cap 行为不变 / kill 中途不丢不超 | S0+T4(已落地) |

> **顺序**:D1(S,独立,零争议)先落止血 → C4(M,独立,需决策①)→ H2(M,建在 S0+T4 上,需决策②)。三者各自独立 PR,可分别 review/merge。本轮整体叠在 LR2(#48)之上。

---

## 7. 五铁律 / kill-test 辩护

- **铁律一(provider 中立)**:D1 的估计落在 adapter seam,loop 不感知;C4/H2 纯 SQL+Python。零 `adapter/` 接口变更。
- **铁律三(拔线)**:C4 的删除按时间窗幂等,中途 kill 下次续删;H2 的 `no_progress_count` 是持久行,Phase -1 每 tick 重评估,cap 仍成立;D1 估计是内存态,wakeup 结束即弃。
- **铁律四(三层持久)**:C4 字面执行"过程密度可回收、结论/通讯密度留存"——清的是 wakeups 指标/已投 outbox(过程),留的是 mailbox/transcript(结论与冷档)。
- **铁律五(mailbox 唯一)**:C4 **绝不**删 `mailbox_messages`;H2 的进展信号读 mailbox/task 行,停-loop 走 final 邮件,无旁路。

## 8. 明确非目标(本轮不做)

- **H3** dispatch/邮件 hop 深度上限 —— 反失控第三腿(跨 actor 环路),Round 4 候选。
- **C1** facts 语义对账 —— 行为型、依赖模型自觉。
- **D2** 公平派发 —— 仅多 agent 规模显现,且每 tick 自愈,优先级最低。
- **B1** 美元天花板 —— 按 owner MVP 约束,不做。
- **park/resume producer(agent 自主 park 等外部事件)** —— **经对抗辩论裁决:现在不建**(2026-06-09)。`park`/`request_resume`/`find_resumable`/`resume` + Phase 0.7 DAO 已就绪但**零生产调用方**。关键发现:parked 任务(`needs_input`)被 `active_owner_agent_ids()`(`sqlite_impl.py`)计入,从而**对 Phase 0 auto-wake 不可见**——即 **park 让 agent 对邮件失聪**(由 `tests/test_task_park_resume.py::test_parked_task_suppresses_auto_wake_for_its_agent` 锁定;fan_in 协调器正因此**刻意不 park**,见 `WORKFLOW_ORCHESTRATION.md` 铁律一(b))。这恰好**反证**了"park 解锁 human-in-the-loop / 等审批"的卖点:等审批时,审批邮件叫不醒它。辩论中连**控方**(被指派论证"该建"的一腿)都自认 `future-mail-after-complete` 已覆盖所有 observed 的多-wakeup 模式。**复活条件(任一即可重提)**:(a) 出现一个 `future-mail` 服务不了、**必须让 task 保持 alive** 的 observed 用例(dashboard 在飞可见 / checkpoint 原子绑定单一 task 行 / parent 链不可断);(b) owner 决定重设 Phase 0,让 high-urgency 邮件穿透 `needs_input` 唤醒 parked agent(需做 starvation/准入分析)。在此之前 park/resume 仅作 kill-safe 惰性原语保留;**parked 任务 cancel 即时生效**等周边一并待其上线再补。
- **不把 cold transcript 回读进 runtime / 不做 facts 向量检索。**

## 9. 决策(已对齐,2026-06-08)

- **决策① C4**:✅ **自动 phase + 手动 CLI 都给**——一个默认关的调度器低频维护 phase(`retention_days>0` 才跑)+ 一个随时手动的 `lyre maintenance` CLI。`retention_days` 默认 **0(关)**,文档建议 60 天;保"每 agent 最近 K 行 wakeups"作为兜底下限。
- **决策② H2**:✅ **work-AND-no-output**——只有"本轮触发的 wakeup 做了实质工作(turns/token 超地板)且无外向产出"才记一次空转(join `wakeups` 指标按 thread 统计);`max_no_progress` 默认 **0(关)**,建议 3。
