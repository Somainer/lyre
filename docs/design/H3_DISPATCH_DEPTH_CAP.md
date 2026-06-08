# Lyre — H3:dispatch 深度上限(反跨-actor 失控)

> **文档定位**:反失控三件套的第三条腿。H1 治 wakeup *内*死循环、H2 治跨 wakeup 的*自驱动 loop* 空转,H3 治**跨 actor 的 dispatch 链失控**——一条 `dispatch_task` 嵌套链(owner→A→B→C→…)今天**无任何深度/环路上限**,一个误判的 agent 能无限往下派子任务。给每个 task 一个 `depth`(metadata),`dispatch_task` 把子 depth 置为父+1,超过上限即**拒绝并要求升级**而非继续递归。
>
> **English one-liner**: The third anti-runaway leg. A `dispatch_task` chain has no depth cap today, so a confused agent can spawn an unbounded A→B→C→… tree. Carry a `depth` in task metadata (child = parent + 1, propagated mechanically), and refuse a dispatch past `max_dispatch_depth` — the agent must escalate to its parent/owner via mail instead of recursing.
>
> **相关**:[`LONG_RUNNING_ROBUSTNESS_2.md`](./LONG_RUNNING_ROBUSTNESS_2.md)(H1 内层死循环)、[`LONG_RUNNING_ROBUSTNESS_3.md`](./LONG_RUNNING_ROBUSTNESS_3.md)(H2 跨-wakeup 无进展闸);[`AGENT_THREADS.md`](./AGENT_THREADS.md)(`thread_id` 也走 metadata 机械传播,H3 的 `depth` 同构)。
>
> **owner 对齐**:2026-06-08。原为 deferred,经 46-agent 对抗式辩论抬入活跃集(rank 3,"唯一未设防的跨-actor 失控腿"),排在 H2 之前。
>
> **状态**:已实现并通过测试(845 测试 + ruff + mypy 绿)。

---

## 1. 问题(对照源码)

`dispatch_task`(`runtime/tools/tasks.py`)给子 task 设 `parent_task_id=ctx.task_id` 并机械传播 `thread_id`,但**没有任何 depth/hop 记录或上限**(全仓库 grep 无 depth/hop)。一条 owner→A→B→C→… 的嵌套 dispatch 链可以无限深;一个误判"我得再派个子任务"的 agent 没有任何 harness 兜底拦它。`mailbox.py` 只有"防瞬时自邮件循环"那一处,管不到 dispatch 树。

## 2. 设计

- **载体**:`task.metadata.depth`(int)。根 task(owner 直派)无 depth → 视为 0。
- **机械传播**(agent 不维护):`dispatch_task` 把**子** depth 置为**父 depth + 1**。父 depth 由调度器在构建 wakeup 的 `ToolContext.extras["task_depth"]` 时从当前 task 的 `metadata.depth` 带入(无额外 DB 读)。
- **上限**:`dispatch_task` 算出 `child_depth`,若 `max_dispatch_depth > 0` 且 `child_depth > max_dispatch_depth` → 抛 `ToolError`,文案明确要求**升级**:"不要再往下派;经 mailbox_send 向 parent/owner 升级,说明卡点与所需"。agent 据此走 mailbox(铁律二:对外行动唯一经 Lyre 工具)升级,而非递归。
- **传播路径**:调度器 `extras["max_dispatch_depth"] = config.max_dispatch_depth`、`extras["task_depth"] = task.metadata.depth`;`dispatch_task` 读 `ctx.extras`。

## 3. 配置 / 默认

`max_dispatch_depth`(`[scheduler]` / env `LYRE_MAX_DISPATCH_DEPTH`,env-beats-toml)。**默认 8**(慷慨——真实 dispatch 树通常 2–3 层),`0` 关闭,负/垃圾→默认 8。是 active-by-default 的安全上限,符合本轮"反失控默认生效"。

## 4. 明确非目标

- **邮件 ping-pong 环路的 hop 上限**:A↔B 互发不是 dispatch 树,需另一套 thread-hop 计数;较罕见,本条只做 dispatch 深度(更清晰、更高价值)。`thread_id` 已是现成载体,留作后续扩展。
- **不引入专门的 escalation 机制**:升级就是 agent 发一封 mail——复用既有 mailbox,无新控制面。

## 5. 五铁律 / kill-test

- **铁律一**:纯 Python;不碰 adapter。
- **铁律三(拔线)**:`depth` 是持久 task.metadata 行;dispatch 拒绝在工具层即时返回,无中间态。
- **铁律四**:无新持久面(复用 task.metadata,同 `thread_id`)。
- **铁律五/二**:升级走 mailbox(唯一对外/跨 agent 通道),无旁路。
