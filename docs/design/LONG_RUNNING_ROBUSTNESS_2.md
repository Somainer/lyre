# Lyre — 长跑健壮性(二):恢复诚实性 + 反失控 + 索引卫生

> **文档定位**:`LONG_RUNNING_ROBUSTNESS.md`(RB-1/2/3)堵住了"压缩"与"记忆膨胀"两条路径;本文补的是另外两类**与负载无关、随运行时长必然显现**的短板——(1) **恢复/终态的诚实性**:lease 只领不续 + 终态写无围栏 → 长 wakeup 被误判崩溃后双 worker 抢同一份 FS 状态、重放不可逆副作用;max_turns 耗尽被误判 `completed` → 假成功污染 supervision 层。(2) **失控的有界化**:wakeup 内零死循环检测,operator 无定向干预手段。三类问题共享一根骨架——一个 **"turn 边界协作停机 → 干净 finalize → 释放租约"** 的 seam,A1(wall 停)、H1(死循环 bail)、B2(operator 取消)全挂其上。外加两项纯机械的记忆索引卫生(C2/C3)。
>
> **English one-liner**: A second workload-agnostic durability round. One shared "stop the wakeup loop cooperatively at a turn boundary → finalize cleanly → release the lease" seam serves three triggers — an operator cancel, a per-wakeup wall deadline, and a within-wakeup dead-loop detector — turning the dormant `renew_lease` into a real at-most-one-live-holder guarantee and giving the operator a surgical per-task stop. Plus: classify max_turns exhaustion honestly (`needs_continuation`, not `completed`) and stop the facts memory index from being an unbounded, unscoped per-prompt tax.
>
> **相关**:[`FOUNDATION.md`](./FOUNDATION.md) 铁律三(kill-test)、铁律五(mailbox 唯一);[`AGENT_RUNTIME.md`](./AGENT_RUNTIME.md) §3.1 参考 loop(本文 A2 是把实现拉回它文档化的 `for…else` 不变量);[`AGENT_THREADS.md`](./AGENT_THREADS.md) T4(有界自驱动:本文 H2/B1 明确推迟,保留 T4 的计数 cap 作兜底);[`LONG_RUNNING_ROBUSTNESS.md`](./LONG_RUNNING_ROBUSTNESS.md)(round 一)。
>
> **owner 对齐**:2026-06-07。明确约束:**MVP 不设硬美元/预算天花板**(难界定合理值),失控只靠 harness 强制的"死循环/无进展即停"。源自一次 38-agent 缺口分析(20 提案 → 15 验证)。
>
> **状态**:**已实现并通过测试**(全部 7 项 S0/A1/A2/H1/B2/C2/C3 落地,`ruff`+`mypy`+827 测试通过)。实现说明见 §12。PR 路线见 §9。

---

## 1. 背景:哪些短板由"运行时长"驱动

Lyre 要跑数月到数年。两类缺陷**不依赖 agent 在干什么**,只依赖累计运行时间足够长:

1. **wakeup 时长分布有肥尾**。慢/被限流的 provider、大上下文多次压缩、多轮委派,都会让某次 wakeup 跑过它 1800s 的租约。跑得够久,"某 wakeup 超租约"的概率趋近 1。一旦租约过期被当成崩溃重派,叠加进程重启清空内存去重表,就是经典的 **lease-without-fencing** 双执行。

2. **大任务会蹭到轮次/上下文上限**。负载随时间漂向更 agentic、更大的任务,越来越多 wakeup 在最后一轮蹭到 `max_turns`;若那轮恰好带 `end_turn`(DeepSeek/Anthropic 常态),就被静默判成"完成"。

3. **没有失控的有界化**。模型偶发死循环(同一工具同参数反复调)、或 operator 想叫停某个跑偏的 task——前者无任何检测,后者无任何定向手段(只能 SIGKILL 整个进程)。

4. **记忆索引单调增长**。facts 索引整份注入每个 agent 的 system prompt;agent 私有 notes 文件还混在 facts/ 里被一起列。agent 群随时间只增不减 → 每个 wakeup 的 prompt 尾部背一笔越来越重、且大多不相关的 token 税,并破坏 prefix-cache。

---

## 2. 问题(对照源码)

### A1 — lease 只领不续 + 终态写无围栏(严重)
`renew_lease`(`sqlite_impl.py:529` + 接口 `repositories.py:149`)**实现完备、有测试,却零生产调用方**(全仓库只有 `tests/test_persistence.py:67-68` 引用)。租约只在 wakeup 起点 `claim_lease` 领一次(`scheduler.py:1281`),`agent_loop.run()` 全程不续。任何 wakeup 跑过 `lease_until` 仍在执行时,Phase-2 `find_expired_leases`(`scheduler.py:320`)就把它当崩溃重派;而进程重启后内存去重表 `_active_subprocesses`(`scheduler.py:184`)是空的(`321`/`347` 的去重失效),第二个 worker 与第一个抢同一份 scratchpad/notes,可重放 `git push`/开 PR 等不可逆副作用。

雪上加霜:终态 `update_status`(`sqlite_impl.py:570`)只有 `WHERE id=?`,**没有** `update_checkpoint`(`:557`)/`release_lease`(`:546`)都带的 `AND lease_holder=?` 围栏。于是一个被顶替的旧 worker 的终态写会**静默覆盖**新持有者。

### A2 — max_turns 耗尽被误判 `completed`
`for turn_idx in range(self.max_turns)`(`agent_loop.py:347`)**没有 `else` 子句**;`final_stop_reason` 每轮被覆盖(`:374`),`result_status` 只看它(`:720-726`)。两个自然出口都用 `break`(干净收尾 `:501`、压缩 thrash bail `:643`),所以"跑满全部轮次后掉出循环"这条路径**与 `break` 出口无法区分**。若末轮 `stop_reason='end_turn'` 且带 tool_use,被截断的 wakeup 被判 `completed` → 任务 `completed`(`scheduler.py:96`)→ 向 supervisor 报 success、顶层任务静默落库不重试。`AGENT_RUNTIME.md` §3.1 文档化的参考 loop 本是"耗尽 → needs_continuation",实现漂移了。

### H1 — wakeup 内零死循环检测
`agent_loop.py` 里没有任何重复调用 / 无进展 / 死循环检测(grep 全空)。唯一的内层兜底是计数型的 `max_turns` 和压缩 thrash bail。模型把同一工具同参数调 24 遍,就是默默烧完轮次走人(还可能撞上 A2 被误判完成)。

### B2 — 无定向 task 取消
`cancelled` 状态的**接收侧已全部接好**:scheduler 透传(`scheduler.py:96`)、终态分类、`task_terminated` 邮件、0001 的 CHECK 都允许它——**只差 `agent_loop` 从不发出它**(`result_status` 只产出 `silent_close`/`completed`/`needs_continuation`)。CLI 没有 `tasks cancel`(只有 `tasks list` `main.py:1827`、`mail cancel` `:1958`);`KillSwitch` 是 chaos 测试专用(`kill_switch.py`);发邮件明确不打断在跑的活(`main.py:913`);软归档(`:1493`)让在途任务跑完。operator 唯一的真·叫停是 SIGKILL 整个 `serve`,连带杀掉所有其他 agent 的在途工作。`task.deadline` 也无处强制。

### C2 — facts 索引不分 scope
`context.py:205` 调 `build_memory_index_for_prompt` **不传** `effective_id`/`persona.name`,而紧随其后的 skills 调用(`:216-217`)两者都传。`MemoryEntry.scope`(`memory.py:78`)被解析,却只在 `format_memory_index`(`:248`)做装饰性后缀渲染——不做任何过滤。整份 facts 索引注入每个 agent。

### C3 — 私有 notes 污染共享索引
per-agent notes 物理上写在 `facts/agent-<id>-notes.md`(`wakeup_summary.py:259`),而 `scan_memory_dir`(`memory.py:129`)遍历 facts/ 时只跳过非 `.md` 和点文件(`:150-152`),**不排除** notes。于是每个 agent 的"Available global memory"里都列着**其他每个 agent 的私有笔记本**(各带 seed.py 给的约 20-30 token 描述)。notes 一创建即存在、永不删除,索引随系统**累计** agent 数无界增长。

---

## 3. 设计骨架:协作停机 seam(stop-request)

一个统一概念:**一个正在跑的 wakeup loop 可被"请求在下个 turn 边界停下、干净 finalize、释放租约"**。三个触发器喂同一个 seam:

| 触发器 | 来源 | 目标终态 |
|---|---|---|
| **operator 取消**(B2) | DB 里的 durable flag | `cancelled` |
| **per-wakeup wall 到点**(A1) | run() 起点算的 monotonic deadline | `needs_continuation` |
| **死循环**(H1) | 内存里的滚动调用指纹 | `needs_continuation` |

**实现**:在**已有**的 turn 边界缝——`for` 顶部(`agent_loop.py:347`)与工具批执行后 `kill_switch.check` 那处(`:558`)——加一个极廉价的 `_check_stop() -> StopReason | None`。命中即 `break`,并把 `result_status` 覆盖成该触发器的目标终态。**收尾完全复用现有路径**:`result_status` 计算(`:720`)→ scheduler 的原子提交(`scheduler.py:1496`,把终态 `update_status` + `task_terminated` 邮件作为一个 commit)→ 释放租约。新增代码只是"算出该不该停 + 该停成什么状态",收尾零新增。

- **协作、非暴力**:只在 turn 边界停,绝不在工具调用中途砍(长 shell 先把当前 turn 跑完)。与既有否决 `brutal_kill`、`kill_switch` 只在 `check()` 点触发的哲学一致。
- **`needs_continuation` → `failed`**:wall/死循环停走既有 `needs_continuation` 分支,scheduler 既有映射(`_WAKEUP_TO_TASK_STATUS`)把它落成 `failed`——诚实、可重派。
- **`cancelled` 终态**:终态、不重派。

各触发器的信号来源见 §4/§6/§7。

---

## 4. A1:lease 心跳续租 + 终态围栏(挂 seam)

**(1) 心跳**:在 `AgentLoop.run()`(进而覆盖 subprocess 模式走的 `_run_task_inline`)起一个后台 task,每 `~lease_duration_s/3` 调 `repos.tasks.renew_lease(task_id, holder_wakeup_id=wakeup_id, duration_sec=lease_duration_s)`;`finally` 里 cancel+await 它。`renew_lease` 的 `WHERE lease_holder=?` **天然自围栏**:若租约已被偷,返回 `False` → 心跳据此触发一次 **stop-request(`needs_continuation`)**,让被顶替的 worker 主动、迅速地停,而不是跑到底再去提交(那个提交会被下面的围栏拒掉,但主动停更省、更干净)。

**(2) 终态围栏**:给 `update_status` 加可选 `holder_wakeup_id`;**仅**在两处终态调用点传(成功 `scheduler.py:1514`、失败 `:1571`),使旧 worker 的终态推进 no-op。wakeup **之前**那些 `update_status("failed")`(`761`/`1183`/`1270`/`1338`)**不加**——那里还没领租约。

**(3) wall 上限**:心跳不能盲续到天荒地老,否则一个"活着但卡死、又不重复调用"的 wakeup(如卡在一个慢外部调用里)会永远续租、永不恢复。所以 run() 起点用 `time.monotonic()` 算一个 per-wakeup wall deadline,turn 边界检查 → 命中即 stop-request(`needs_continuation`)。**用 monotonic**,NTP/DST 跳变扰不动它。

> 取舍(待定值):wall 默认值。MVP 建议给一个**慷慨的具体默认**(如 `3 × lease_duration_s`),`0` = 关。理由:A1 的正确性需要*某个*兜底让卡死 wakeup 终归会停;默认全关会留一个"心跳永续→永不恢复"的洞。

激活的是已写已测的死代码;无 schema、无围栏新列。

## 5. A2:max_turns 诚实归类

恢复 `for…else` 不变量:只有当循环**走完全部迭代**、既没走干净收尾 `break`(`:501`)也没走压缩 thrash `break`(`:643`)时,置 `hit_max_turns=True`。`hit_max_turns` 时**无视 stop_reason 强制 `result_status='needs_continuation'`**,接既有 `needs_continuation→failed→task_terminated` 路径,使截断可见、可重派。**`hit_max_turns` 压过 `silent_close`**(owner 对齐):被截断的活比静默收尾更该被暴露。纯 runtime,无 schema。

## 6. H1:wakeup 内死循环守卫(挂 seam)

对每个工具调用算指纹 `(tool_name, 规范化-JSON(args))`,在 wakeup 内存里维护最近调用的滚动记录:

- **连续 K 次同指纹** → 注入**一次** nudge("你在重复 `X(...)`,换路子或停止调工具来结束本 wakeup");
- nudge 后仍继续同指纹 → **stop-request(`needs_continuation`)**。

K 经 config(`LYRE_LOOP_REPEAT_THRESHOLD` / `[runtime] loop_repeat_threshold`,env-beats-toml,默认如 5,`0`=关)。**为何同参数重复是安全的退化信号**:同一 wakeup 内对同一对象反复同参数调用,状态在一次同步 wakeup 内不会变——这本身就是退化;而"读很多文件"是**不同** args、不同指纹,不误伤。纯内存、无 schema、wakeup 结束即弃。

## 7. B2:operator 协作取消(挂 seam)

**(1) 写请求**:`lyre tasks cancel <task_id> [--reason]`(CLI)+ dashboard 一个按钮,写一个 **durable cancel-requested flag**(放 `tasks.metadata` JSON;若想要可查询/可索引则就地在 0001 加一列)。

**(2) 观测**:运行中的 wakeup 在 turn 边界廉价读该 flag(可搭 A1 心跳那次 DB 往返一起读,省一次 query)。命中 → **stop-request(`cancelled`)** → 走 seam 收尾(原子提交 + `task_terminated`)。

**(3) kill-safe**:flag 是持久行。取消请求与观测之间 SIGKILL → flag 仍在 → 恢复/重派的 wakeup 再读到、再停。honored 时在**同一原子终态提交**里清掉 flag(`cancelled` 是终态、不重派,故无"重派被新任务误取消"问题;task id 唯一也保证不串)。

取消的是**一个 task,不是 agent**——agent 继续接别的活。这是 operator 在无人值守时对单个跑偏 task 的精准逃生阀,免去"SIGKILL 全进程、连累所有在途工作"。

## 8. C2 + C3:记忆索引卫生(纯机械)

**C2 — 索引按 scope 过滤**:把 skills 的 scope 文法(`skills.py`:`"global" | "persona=<name>" | "agent=<id>"`)抬进 facts 层。给 `MemoryEntry` 加 `applies_to(agent_id, persona_name)`;在 `format_memory_index`/`build_memory_index_for_prompt` 加一道过滤;把 `effective_id`+`persona.name` 从 `context.py:205` 那个**已在作用域里**(紧随的 skills 调用就在用)的调用点 thread 进去。**关键不回归点**:现存 facts 用**自由格式** scope 串(如 `"lisa-lang"`,见 `memory.py:47`、`test_memory.py:74`),`SkillScope.parse`(`skills.py:78`)会对它抛错——所以**包一层**:不可解析/自由格式的 scope **回退为 global(永远适用)**,保住现有测试与"无 scope 即全局"的语义。

**C3 — notes 排除出共享索引**:`scan_memory_dir`(`memory.py:150-153`)walk facts/ 时排除匹配 `agent-*-notes.md` 的文件——私有笔记本本就经 identity preamble 推给其属主、也能 `read_memory` 取,无须再出现在每个 agent 的共享索引里。一行过滤 + 一个"notes 不在索引中"的测试。
> 注:**不**碰 `read_memory` 的读路径——Lyre 的隔离是整运行时 Docker 包裹于单一 owner 信任域内(`AGENT_CONTRACT.md:198`),不存在 agent 间机密边界;这里纯粹治索引膨胀,不是做 ACL。

---

## 9. 分 PR 路线

| PR | 内容 | 关键离线测试 | 依赖 |
|---|---|---|---|
| **S0** | 协作停机 seam:turn 边界 `_check_stop()` + `StopReason`(reason→target_status)+ 复用既有 finalize/原子提交 | stop-request 在 turn 边界生效 / 覆盖 result_status / 不在工具中途停 / 收尾走既有提交+task_terminated | — |
| **A1** | lease 心跳(`renew_lease` 后台续 + 偷租自停)+ 终态 `holder_wakeup_id` 围栏 + per-wakeup wall(monotonic)经 seam 停 | 心跳续租保活长 wakeup / 偷租→renew False→自停 / 终态围栏:旧 worker 写 no-op、wakeup 前的不加 / wall 到点 needs_continuation / kill 中途恢复一致 / subprocess 模式同覆盖 | S0 |
| **A2** | `for…else` 还原 `hit_max_turns` → 强制 `needs_continuation`(压过 silent_close) | 耗尽 on end_turn→needs_continuation 而非 completed / 干净 break 不误判 / thrash break 不误判 / 压过 silent_close | — |
| **H1** | wakeup 内重复同调用检测:指纹连续 K→nudge→仍续→经 seam 停 | 连续 K 同指纹触发 nudge / nudge 后续→needs_continuation / 不同 args 不触发 / 阈值 0 关闭 | S0 |
| **B2** | `lyre tasks cancel` + dashboard 按钮 + durable flag;turn 边界观测→经 seam 停 `cancelled`;honored 即清 flag | cancel→下个 turn 边界 cancelled+task_terminated / 请求-观测间 SIGKILL 重派后仍取消 / honored 清 flag / 取消 task 不杀 agent | S0 |
| **C2** | facts 索引按 scope 过滤(抬 skills 文法;自由格式 scope 回退 global) | 按 agent/persona 过滤 / 自由格式 scope 仍全局可见(现有测试不变) / 无 scope→全局 | — |
| **C3** | `scan_memory_dir` 排除 `agent-*-notes.md` | notes 不入索引 / 普通 facts 仍入 / 属主仍能 read_memory 取 | — |

> **依赖序**:S0 是 A1/H1/B2 的共同前置,建议同一 PR 簇推进。A2、C2、C3 三者独立、可并行先落(都是 S,即时止血/纯赢)。A1 优先级最高(会致双执行 + 静默覆盖)。

---

## 10. 五铁律 / kill-test 辩护

- **铁律一(provider 中立)**:全部纯 Python + SQL + FS;不碰 `adapter/`。stop-request、指纹、wall 都活在 runtime/loop 内。
- **铁律三(kill-test)**:A1 把"恢复"从损坏源变回真·至多一个活持有者(心跳续 + 终态围栏)。seam 的停机走**既有原子提交**(终态 update_status + task_terminated 一个 commit,`scheduler.py:1496`),中途 kill 由 `find_expired_leases` 恢复。B2 的 flag 是持久行,请求-观测间 kill 后重派仍生效;wall/指纹是内存态,kill 后新 wakeup 重新计起,无残留。
- **铁律四(三层持久)**:无新持久面(B2 复用 `tasks.metadata` 或单列;wall/指纹 local-hot 内存,wakeup 结束即弃)。
- **铁律五(mailbox 唯一)**:停机的对外信号全走既有 `task_terminated` 邮件;无新通讯通道。B2 的取消请求是 owner→runtime 的控制面(经 DB flag),不是 agent 间旁路。

## 11. 明确非目标

- **不设硬美元/预算天花板**(B1)——owner MVP 约束;失控只靠 harness 的死循环/wall/手动取消。
- **不做跨 wakeup"无进展就停"语义闸**(H2)——保留 T4 的计数 cap 作兜底;语义进展判定(区分等待型心跳 vs 空转 thrasher)留待以后。
- **不做 dispatch/邮件 hop-深度上限**(H3)、**DB 保留/VACUUM/WAL**(C4)、**facts 语义对账**(C1)、**压缩兜底 token 估计**(D1)、**公平派发**(D2)——均在本轮之外,排名分析见 38-agent 缺口分析存档。
- **不改 `_MAX_COMPACTIONS` / 不碰 `read_memory` ACL / 不引入 facts 检索排序**。
- **不暴力 kill wakeup**——只协作式 turn 边界停。

---

## 12. 实现说明(落地后补)

**已落地**:S0(`agent_loop._StopRequest`/`request_stop`/turn 边界检查/finalize 优先级)、A1(`scheduler._lease_heartbeat` + `update_status(holder_wakeup_id=)` 围栏 + `wakeup_wall_budget_s`)、A2(`for…else hit_max_turns`)、H1(`loop_repeat_threshold`,默认 5)、B2(`tasks.request_cancel/get_cancel_request` + `cancel_check` + `lyre tasks cancel`)、C2(`MemoryEntry.applies_to` + 索引过滤)、C3(notes 排除)。

**对抗式评审后追加的两处硬化**(12-agent diff review):
1. **cancel 标记不随重启传播**:B2 把 cancel 存在 `tasks.metadata`,而 supervisor 重启(`_supervise_ephemeral`)逐字拷贝 metadata → 一个 `permanent`/`transient` ephemeral 被取消后会"带着取消标记重生"→ turn 0 又被取消 → 重启风暴自杀。修复:重启拷贝 metadata 前 strip 掉 `cancel_requested/cancel_reason`(`_metadata_without_cancel`)。
2. **终态 task_terminated 邮件随 update_status 一起围栏**:A1 只围栏了 `update_status`,但同一提交里的 `_emit_task_terminated_mail` 没围栏 → 租约被偷时,被顶替 worker 仍发一封 phantom `failed`,经 `external_id` 去重把新持有者的真实结果挤掉(仅 subprocess 模式 + 跨整个租约窗口的 stall 可达)。修复:`update_status` 改回传 bool(是否真改了行),终态邮件 gate 在该 bool 上。

**B2 dashboard 按钮**:已做。task detail 页(`/tasks/{id}`)对非终态任务显示「Request cancel」表单(可填 reason),POST `/tasks/{id}/cancel` → `request_cancel` → 303 回 detail;已请求时显示「Cancel requested」banner 而非表单。这是 dashboard 除 `/send` 外唯一的写路径。

**本轮唯一保留的小尾巴**:
- **parked(`needs_input`)任务的 cancel 语义**:当前 cancel 对 parked 任务不立即生效(评审 finding 4,LOW)。但 park/resume 机制目前无任何生产调用方(`park()` 零 caller),该状态不可达,故为潜在限制而非现 bug;待 park/resume 上线时补 `resume_ready` 提升。
