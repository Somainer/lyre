# Lyre — 编排健壮性:fan-in 失败可见 + typed-result 强制 + 轮次预算

> **文档定位**:来自一次**生产** fanout/fan-in 失败复盘(owner 自检报告)。三个真·runtime 缺口让"子任务静默失败、协调者不知情"成为可复现事故——(O1) fan-in 失败腿**零协调者信号**,barrier 只数已交付 typed 结果,睡到 24h deadline;(O2) 腿**完成却没交 typed result** = 静默死腿;(O3) `max_turns` 硬编码 24、`tier_overrides` 铺了管道零消费、`needs_continuation→failed` 无续跑。A2(已合并)让"截断"变诚实,但把**通知**与**续跑**都 punt 了——本文正是补那几层。
>
> **English one-liner**: Three production-grounded orchestration gaps. A2 made turn-exhaustion classify honestly but delivered nothing to the coordinator. O1 turns a failed/abandoned fan-in leg into a typed barrier event so the group resolves at quorum instead of the 24h deadline; O2 fails-loud a leg that completes without submitting its typed result (feeding O1); O3 makes the dormant `tier_overrides` live (per-task turn budget) plus a bounded, progress-gated auto-continuation of a pure max_turns truncation — reusing the A1/H1 stop seam so anti-runaway still holds.
>
> **相关**:[`LONG_RUNNING_ROBUSTNESS_2.md`](./LONG_RUNNING_ROBUSTNESS_2.md)(A2 截断诚实 + S0 停机 seam,O3 复用);[`WORKFLOW_ORCHESTRATION.md`](./WORKFLOW_ORCHESTRATION.md)(fan-in barrier / task_terminated 监控,O1/O2 扩展);[`AGENT_THREADS.md`](./AGENT_THREADS.md)(H2 无进展闸,与 O3 续跑同属"进展门控")。
>
> **owner 对齐**:2026-06-08。范围 **O1+O2+O3**(P0+P1),**O4 缓**。顺序:**O1+O2 先于 Round-3 的 H2**,O3 在 H2 之后。源自 11-agent 复盘(5 findings → 3 真缺口 + 2 非缺口)。
>
> **状态**:设计已对齐,待实现。

---

## 1. 背景:生产 fanout 事故的三个根

dispatcher 并行派三条研究腿;结果:架构腿完成但只发普通邮件(无 typed result),另两条深调研腿耗尽 turn 进 `needs_continuation` 最终失败;**fan-in delivered 始终 0/3,dispatcher 既没拿到 barrier-ready,也没收到任一失败腿的系统告警**,只能事后自己补录。这不是 primitive 缺失,是**编排层的通知/校验/预算**三处脆弱叠加。

## 2. 问题(对照源码)

### O1(P0)— fan-in 失败腿零协调者信号
`_emit_task_terminated_mail` 对带 `fan_in_group` 的 task **提前 return**([scheduler.py:946](../../src/lyre/scheduler/scheduler.py))——这是有意的(避免每个子腿都 normal-urgency 唤醒协调者)。但 barrier 侧 `count_fan_in_results`([sqlite_impl.py:1472](../../src/lyre/persistence/sqlite_impl.py))**只数已交付的 typed result 邮件**,而 `_resolve_fan_in_barriers`(Phase 0.5,scheduler.py:579-666)**从不读 roster / tasks.status**。于是一条 failed 腿与一条仍在跑的腿**无法区分**,协调者睡到 per-group deadline(可达 24h)或 `LYRE_FANIN_MAX_AGE` 才醒。

### O2(P0)— 腿完成却没交 typed result = 静默死腿
fan-in 腿可以做完工作、发一封普通 `mailbox_send`、却**从不调** `mailbox_send(result_for=…)`,然后正常 `completed`。终态提交(scheduler.py 围栏 commit)**无任何 typed-result 校验**;barrier 永不 +1;task_terminated 被 O1 那条 946 suppression 吞掉;默认 `temporary`/`transient` reaper 也不会 restart 一条 completed 腿。A2 只碰"截断",不碰这条"干净完成"路径。

### O3(P1)— 轮次预算 + 续跑双缺
- **预算**:`max_turns` 是**硬编码 24**([agent_loop.py:316](../../src/lyre/runtime/agent_loop.py));调度器 build `AgentLoop` 处**根本没传** max_turns;config 无该字段。`tier_overrides` 在 models / DAO 都铺好了([models.py:195](../../src/lyre/persistence/models.py)、round-trip 在 sqlite_impl)却**零 runtime 消费者**。
- **续跑**:`needs_continuation→failed` 是终态;`failed` 永不被重领(`find_pending` 只看 pending、`find_expired_leases` 只看 in_progress),尽管 `report_progress` 存了 checkpoint、`context.py` 会把它 re-seed 进下一 wakeup。

### 非缺口(纠正报告)
- **非 fan-in 子任务失败→parent 通知**:**已可靠**——`_resolve_terminated_task_supervisor` 把终态邮件路由到 `parent_task_id` 的 agent(archived 则回落 owner),A2 + 这个解析已覆盖、且 LR2 已 kill-safe 围栏。只缺测试。
- **list_tasks 返回空**:过滤器用法问题(空过滤结果合法),非 bug。→ agent-discipline。

## 3. O1:fan-in 失败腿 → typed barrier 事件(Phase-0.5 reconciliation)

Phase 0.5 新增一步:join `fan_in_members.child_task_id → tasks.status`。对一条**终态非 completed**(failed/cancelled)、且**无 result 邮件**的腿,向协调者插一封**幂等的合成 result 邮件**(sentinel:`_leg_failed=true`、`reason=<status>`,external_id `fanin:<group>:<leg>:failed`)。

- 该 sentinel **被 `count_fan_in_results` 计入** → barrier 在 **quorum** 提前 resolve,而非等 deadline。语义从"所有腿成功"放宽为"**所有腿到达终态(成功或失败)**"——协调者由此决定重派/带伤推进/升级。
- `read_fan_in_results` 把 sentinel 识别为失败腿;`fan_in_results` 工具([fan_in.py](../../src/lyre/runtime/tools/fan_in.py))新增 `failed_legs`(各带 reason),与既有 `missing_legs` 并列。
- **保留** 946 的 suppression(不改 task_terminated 对 fan-in 的静默);失败信号只走 barrier 通道。插入**用与 emit 处同一套 A1 lease 围栏**,superseded worker 伪造不了 sentinel。

## 4. O2:fan-in 完成前 typed-result 校验(失败要响)

在围栏终态 commit 处(`still_holder` 的 `update_status` 之后),当 `metadata.fan_in_group` 存在且 `task_status==completed`:校验该腿是否真有 typed result——`count_fan_in_results`(已投递)**或** 新增 `repos.outbox.has_pending_fan_in_result(task_id, group_id, leg_key)`(未投 outbox,`dispatched_at IS NULL`)。两者皆无 → **降级为 `failed`**,`failure_reason=fan_in_no_typed_result`。降级后自然喂给 O1 的 reconciliation(同一协调者通知路径)。`member_for_task` 直接按 `fan_in_members.child_task_id` 查。

> O1 与 O2 互补且不同:O2 把"干净完成却无结果"的腿**先降级为 failed**,O1 再把**任何终态失败腿**变成 barrier 事件。

## 5. O3:per-task 轮次预算 + 有界自续跑

**(1) 让 `tier_overrides` 生效**:config 加命名 tier(如 `light`/`research`/`deep`,各带 `max_turns`),走既有 env-overridable runtime 旋钮范式;`dispatch_task`/`TaskSpec` 接受 `tier` 或显式 `tier_overrides.max_turns`;调度器 build 处解析 `effective_max_turns` 传给 `AgentLoop`(今天这里漏传)。

**(2) 有界自续跑**(`tier_overrides.max_continuations`,默认 2,0=关):在 `needs_continuation` 的终态写处,**仅当**——是**纯 max_turns 截断**(`self._stop_request is None`,即非 H1/cancel/wall 停)∧ 未超 `max_continuations` ∧ 有 checkpoint ∧ **有进展**——把 task 重置为 `pending` 并 `continuations_used += 1`;否则落 `failed`(fan-in 腿则喂 O1/O2)。

- **反失控不破**:每次续跑仍受 H1(死循环)/A1(wall)/H2(无进展)约束;只对"诚实截断且在前进"的任务续命,绝不对停机 seam 触发的停止续跑。
- **无新通讯原语、无美元预算**(守 owner MVP 约束)。与 H2 同属"进展门控",复用同一 checkpoint/停机语义。

## 6. 验收(离线 mock)

`test_fanin_dispatcher_sees_failed_leg_no_result_and_continuation_failure`:FakeAdapter + in-memory WAL,协调者开 group(`expect_replies=3, quorum=3`),三腿各经真实 `_run_task_inline` commit:
- **腿0**:正常 `mailbox_send(result_for=g, leg_key=0, result=valid)` → 跑 OutboxDispatcher → `count==1`。
- **腿1**:普通 `mailbox_send`(无 `result_for`)→ **O2 降级 failed**(`fan_in_no_typed_result`,重读行验证)→ **O1 插 sentinel** `fanin:g:1:failed`。
- **腿2**:FakeAdapter 永远回 tool_use → 撞 max_turns、`_stop_request is None` → `needs_continuation`;`max_continuations=0` → 落 `failed`(不续跑)→ Phase-0.5 插 `fanin:g:2:failed`。
- 一次 resolve tick 后断言:**(1)** `count_fan_in_results==3`(1 真 + 2 sentinel)→ barrier 在 **quorum** fire(`trigger=quorum`,不靠 deadline);**(2)** 协调者收到高优 ready 邮件,`fan_in_results.failed_legs==[1,2]` 各带 reason;**(3)** `missing_legs==[]`。**幂等**:resolve 跑两遍不重复 sentinel、count 仍 3。**围栏**:holder 不匹配的 superseded emit 伪造不出 sentinel。控制组:关掉 reconciliation 时 count 停在 1、只能靠 ttl 超时 resolve。

## 7. 分 PR / 顺序

| PR | 内容 | 关键离线测试 | 优先 |
|---|---|---|---|
| **O1** | Phase-0.5 失败腿 reconciliation + sentinel result + `fan_in_results.failed_legs` | 失败腿→sentinel→quorum 提前 resolve / 幂等 / 围栏 / 关掉则只 ttl resolve | **P0** |
| **O2** | 完成前 typed-result 校验 → 无则降级 failed(`fan_in_no_typed_result`)+ `has_pending_fan_in_result` DAO | 无 result 的 completed 腿降级 failed / 有 result 不降级 / 喂 O1 | **P0** |
| **O3** | `tier_overrides` 生效(config tier + dispatch + build 处传 max_turns)+ 有界自续跑(`max_continuations`,纯截断+进展才续) | per-task max_turns 生效 / 续跑仅纯截断+进展 / 超 cap 落 failed / H1/cancel/wall 停不续 | **P1** |

> **全局顺序**(owner 定):`D1✅ → C4✅ → O1 → O2 → H2 → O3`。O1+O2 是 P0 生产止血,插在 Round-3 的 H2 之前;O3 在 H2 之后。`test_fanin…` 三腿 mock 在 O1+O2 落地后即可作为联合 anchor。

## 8. 五铁律 / kill-test 辩护

- **铁律一**:全部纯 Python+SQL;不碰 `adapter/`。
- **铁律三(拔线)**:O1 的 sentinel 插入幂等(`fanin:group:leg:failed` external_id + mailbox UNIQUE),中途 kill 下次 Phase-0.5 续插不重;O2/O3 的状态改写走既有围栏终态 commit;O3 续跑只是把 task 重置 pending(持久行),kill 后下个 tick 重评估。
- **铁律四**:无新持久面(continuations_used 可入 checkpoint 或 metadata;tier_overrides 已是 task 列)。
- **铁律五(mailbox 唯一)**:O1 的失败信号走 fan-in 既有 result-mail 通道(barrier 唯一通信面),不另起旁路;task_terminated 对 fan-in 仍静默。

## 9. 明确非目标

- **O4**(task_terminated 推送邮件带 child checkpoint,P2/S)—— 本轮缓;独立小改(重读终态行塞 metadata)。
- **非 fan-in 失败→parent 通知**:已可靠,仅补测试(可并进 O1 的测试集)。
- **list_tasks 空结果** + **typed-result 必交**:**agent-discipline**(persona/工具描述各一行),非 runtime;O2 是 typed-result 的真正强制。
- **不引入**:新 fan-in 语义(quorum 仍是数,只是把失败腿计入终态)、美元预算、无界续跑。
