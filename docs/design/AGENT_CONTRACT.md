# Lyre — Agent 接口契约

> **文档定位**：定义 `Agent` 接口的字段、生命周期、约束。Lyre 中**所有** agent（不论扮演 leader / worker / reviewer 等任何角色）共用此契约——角色由 persona 决定，机制不预设角色差异。任何 agent 后端（自写 agentic loop、第三方 coding agent、开源模型等）接入 Lyre 都须满足此契约。
> **相关**：[`FOUNDATION.md`](./FOUNDATION.md) 五条铁律；[`TRANSACTION_BOUNDARIES.md`](./TRANSACTION_BOUNDARIES.md) 事务边界。
>
> **Implementation correction (2026-06-10)**: §2 (steps 1/7/9/11) and §4.1–§4.5 / §4.8 describe the v0.x plan — fork-subprocess sandbox, Unix-socket gateway, Tier-matrix enforcement — which was **not built**. As built: tools dispatch **in-process** in the wakeup loop (`agent_loop._dispatch_tool`); `src/lyre/mcp_server/` is an empty stub; `allowed_lyre_tools` is enforced when building the LLM tool list (`ToolRegistry.specs_for` in `agent_loop`) and again at dispatch (`_dispatch_tool` allowlist check); containment = the shell/python env allowlist (strips `ANTHROPIC_*` / `LYRE_*`, **deliberately forwards `GH_TOKEN` / `GITHUB_TOKEN`** — §4.1's claim that GH_TOKEN is scrubbed is wrong) + single-owner trust (`CAPABILITY_DISCOVERY.md` §3). The per-task tmpdir and ephemeral SSH key DO exist (`runtime/worktree.py` scratch dir under `object_store/worktrees/{task_id}/`; `git_context` provisions a per-task key + ssh-agent for git ops) — but only as working artifacts, **not** as the containment boundary §4.1 describes: no env-scoped fork sandbox, no cwd jail. Tier enforcement (§4.8) never went beyond `report_side_effect` self-reporting. Drift of the §4.4 tool table vs the real registry (`src/lyre/runtime/tools/builtin.py`, 25 tools):
> - **Never built**: `load_skill` / `propose_skill` / `approve_skill` / `propose_persona` / `approve_persona` / `request_review` / `mark_pr_reviewed` / `query_local_hot_summary` (referenced in §2 / §3.5 / §4.6 here and in AGENT_RUNTIME §4 / PERSONAS.md). Skill bodies are read via `read_memory`; skill governance is plain file moves under `~/.lyre/skills/`.
> - **Registered but missing from the table**: `mailbox_react`, `fan_in_open` / `fan_in_status` / `fan_in_results` / `fan_in_cancel`.
> - §6.2 "send 不立即投递…COMMIT POINT 时一起原子提交" is superseded — sends commit to outbox **at tool time** (see the TRANSACTION_BOUNDARIES.md banner). The §6.3 read-state semantics are current.

---

## 目录

1. [Agent 是什么](#1-agent-是什么)
2. [唤醒生命周期](#2-唤醒生命周期)
3. [接口契约（数据结构）](#3-接口契约数据结构)
4. [运行环境与工具集（铁律二）](#4-运行环境与工具集铁律二)
5. [无状态约束（铁律三）](#5-无状态约束铁律三)
6. [Mailbox 交互（铁律五）](#6-mailbox-交互铁律五)
7. [Provider 适配（铁律一）](#7-provider-适配铁律一)
8. [失败与重试](#8-失败与重试)
9. [度量](#9-度量)
10. [Git hosting 与 hosting-specific 行为](#10-git-hosting-与-hosting-specific-行为)
11. [v0.4 已识别但待解决的问题](#11-v04-已识别但待解决的问题)

---

## 1. Agent 是什么

> "Agent = 对持久状态的一次短暂求值"——[`FOUNDATION.md §3.4`](./FOUNDATION.md#34-铁律三拔线测试是架构对错的判据)

Agent 是 Lyre 中**唯一**的执行实体抽象。不存在"Leader 进程"和"Worker 进程"两种实体——只有"Agent 进程"一种。一个 agent 实例就是**一次唤醒**：拿一个任务 / 角色、读必要的持久状态、想 / 调工具 / 干活、写回持久层、消亡。

**Agent 之间的差异由 persona 决定**：

- `leader` persona → 调度、派活、对 owner 报告
- `worker-maintainer` persona → 改代码、跑测试、提 PR
- `reviewer` persona → 审 PR、给意见
- 未来任意新角色 → 加一个新 persona，**不用造新抽象**

| 维度 | 契约 |
|---|---|
| 输入 | persona 定义 + task + 装配好的 context + 允许使用的工具集 + mailbox 句柄 + 计量句柄 + （可选）上次拔线的 checkpoint |
| 输出 | 产出物（artifacts 引用）+ 触发的事件 / mailbox 消息 + token 与时间计量 + （可选）新的 checkpoint |
| 硬要求 | **无状态**——进程死亡后 in-memory 状态全丢，恢复全靠 checkpoint + 持久层 |

---

## 2. 唤醒生命周期

```
[1] Wakeup           调度器触发；分配 task_id；Lyre 主进程 fork agent subprocess
                     subprocess cwd = per-task tmpdir，env = 清洗后的最小 env
[2] Acquire lease    在持久层获取任务租约；同一时刻同任务只有一个 lease 持有者
[3] Load persona     从 global 读 persona spec
[4] Read checkpoint  如有上次拔线遗留，加载续做点
[5] Assemble context 按 FOUNDATION §3.5 四步组装（persona profile → local-hot → global facts → cold pointers）
[6] Read inbox       读自己的 mailbox（默认拉 unread；mailbox_read 工具自动写 read_at）
[7] Execute loop     思考 → tool call → observe → ...；可多轮迭代
                     可调 Lyre 工具（mailbox_send / report_progress / request_review 等，走 Unix socket gateway）
                     可在 subprocess 内自由 shell（git / 语言工具链 / 任意命令）
[8] Prepare outputs  本地组装 artifacts、emitted messages、新 checkpoint
[9] COMMIT POINT     原子事务写持久层（详见 TRANSACTION_BOUNDARIES.md §2-3）
[10] Release lease   或刷新 lease（如任务未完成需要下一轮唤醒续做）
[11] Terminate       Agent subprocess 退出；Lyre 主进程 reap 子进程、rm -rf tmpdir、
                     kill task-local ssh-agent、异步撤公钥
```

**关键约定**：

- 步骤 1-8 之间任何时点的 kill 都不影响系统正确性——只丢"本次唤醒已做但未 commit 的工作"
- 步骤 9 是唯一的提交点（atomic across stores），通过 outbox 模式实现（详见 [`TRANSACTION_BOUNDARIES.md`](./TRANSACTION_BOUNDARIES.md)）
- 步骤 10 之后任何 kill 都不会丢工作
- **Step 7 Execute loop 的实现机制**（asyncio + streaming + mid-loop 中断 + MCP gateway + LLMAdapter 抽象）定义于 [`AGENT_RUNTIME.md`](./AGENT_RUNTIME.md)。本契约只要求"loop 是 asyncio + streaming + 支持 blocker mailbox 中断 cancel LLM"作为外部行为约定

---

## 3. 接口契约（数据结构）

### 3.1 Input

```ts
AgentInput {
  task_id:         ID
  persona:         PersonaSpec
  task:            TaskSpec
  context:         AssembledContext
  tools:           ToolManifest
  mailbox_handle:  MailboxHandle
  metering_handle: MeteringHandle
  checkpoint?:     WakeupCheckpoint   // 首次唤醒为 null；续做时由调度器提供
}
```

### 3.2 PersonaSpec

```ts
PersonaSpec {
  id:               ID
  name:             string                  // 给人看的名字，如 "leader" / "maintainer-py" / "reviewer"
  role_description: string                  // 自由文本，"你是谁、你的职责"
  system_prompt:    string                  // 预制 prompt，可含策略提醒、Tier 矩阵说明、hosting-specific 注入（详见 §10）
  model_routing?:   ModelRoutingSpec        // owner 制定、leader 解析的 provider / 模型偏好
}
```

> Persona 长期档案存 global（[FOUNDATION §3.5](./FOUNDATION.md#35-铁律四持久层按作用域分三档)）；这里传入的是为本次唤醒拉出的快照。

**Persona 决定 agent 的一切角色行为**——能调哪些 Lyre 工具、是否分配 git 工作目录、走默认 owner→agent 路由的哪个位置——都由 persona spec 配置。机制层面对 persona 无预判。

### 3.3 TaskSpec

```ts
TaskSpec {
  id:              ID
  goal:            string                   // 自由文本，"请把 X 升级到 Y"
  acceptance:      string                   // "测试通过 + 提一个 PR" —— 验收标准
  parent?:         TaskID                   // 子任务时填
  deadline?:       time
  lease_duration?: duration                 // per-task 可配（默认 30 min；长任务可设更长）
  tier_overrides?: TierOverrides            // 可覆盖默认 Tier 矩阵
}
```

### 3.4 AssembledContext

> 装配顺序严格按 [`FOUNDATION.md §3.5`](./FOUNDATION.md#35-铁律四持久层按作用域分三档) 的四步。

```ts
AssembledContext {
  persona_profile: ProfilePayload            // step 1：persona 基础档案（global）
  task_state:      TaskHotState              // step 2：当前任务进度与现场（local-hot）
  task_facts:      FactsPayload              // step 3：任务相关公共事实（global 向量检索）
  cold_pointers?:  ArchivePointer[]          // step 4：极少数情况下的冷归档指针
}
```

### 3.5 ToolManifest

```ts
ToolManifest {
  lyre_tools:        LyreToolSpec[]          // 走 Lyre gateway 的工具（mailbox / progress / request_review / load_skill / propose_skill 等）
  available_skills:  SkillRef[]              // 仅传 frontmatter（progressive disclosure）；agent 用得上时调 load_skill 取 body
  process_env:       ProcessEnvSpec          // subprocess 的 cwd（tmpdir）、env vars 白名单、ssh-agent socket 路径等
  tier_policy:       TierPolicy              // 当前任务的 Tier 矩阵
  // budget 字段 MVP 不存在（Q7：预算控制推下一期）
}

SkillRef {
  name:         string                       // 例如 "apply-dependency-upgrade"
  description:  string                       // 一行摘要
  triggers:     string[]                     // 触发关键词或场景
  scope?:       string                       // 限定生效范围（如某 repo）
}
```

`lyre_tools` 列举的是 Lyre 实现并通过 Unix socket gateway 暴露的工具。
`available_skills` 是**progressive disclosure** 的载体——context 装配时只塞 frontmatter（每条几十 token），agent 判断需要时调 `load_skill(name)` 取完整 body 注入 context。详见 [§4.7 Skills 工作流](#47-skills-工作流hermes--pi-对齐)。
`process_env` 决定 agent subprocess "看到"什么环境——cwd 锁在 per-task tmpdir、env vars 白名单清洗、ssh-agent 指向 task-local socket。**Shell 与命令工具不在此处枚举**（agent 用 host 上的，可任意调用）。

### 3.6 Output

```ts
AgentOutput {
  status:            "completed" | "needs_input" | "failed" | "needs_continuation"
  artifacts:         ArtifactRef[]           // 持久层指针，不是 blob 本体
  emitted_messages:  MessageEnvelope[]       // 要写给其它 actor 的 mailbox 消息（走 outbox）
  metering:          MeteringResult
  checkpoint?:       WakeupCheckpoint        // status = needs_continuation 时必填
  failure?:          FailureReport           // status = failed 时必填
  self_reported_side_effects?: SideEffectReport[]  // agent 自报的进程内已发生的外部副作用（如已 push 分支、已开 PR），用于 Tier 1 检测
}
```

- `completed` → 任务完成
- `needs_input` → 卡在 `urgency=blocker` 消息上，等回复；调度器将 agent 终止，待回复后重唤新 agent
- `failed` → 不可恢复错误，请 leader 处理；失败计数 +1
- `needs_continuation` → 任务未完但本次唤醒已达提交点，调度器可立即/稍后唤醒下一轮续做

---

## 4. 运行环境与工具集（铁律二）

> **铁律二修订版**（FOUNDATION §3.3）：Lyre 与外部世界的**所有交互**通过 Lyre 定义和实现的工具集。Agent 是 Lyre 派生的 subprocess，运行在 per-task tmpdir 内，agent 内可拥有任意 shell——安全靠 scoped credentials + 环境清洗 + 可选的 Lyre 整体容器化，不靠 per-agent 容器。

### 4.1 Agent 运行环境（MVP = 裸 subprocess + tmpdir）

每次 agent 唤醒是 Lyre 主进程派生的 **subprocess**——**没有 per-agent 容器**。Agent 跟 Lyre 同 OS 用户、同文件系统命名空间，用 host 上已装的 git / node / python / 等工具链。具体形态：

- **工作目录**：per-task tmpdir（如 `/tmp/lyre/task-{id}/`），任务结束 `rm -rf`
- **环境变量**：最小化 env，清空 `LYRE_*` / `GH_TOKEN` / `ANTHROPIC_API_KEY` / `AWS_*` 等敏感项；只注入本次任务必需的
- **SSH 凭据**：per-task ephemeral ed25519 keypair，挂在 task-local ssh-agent 上；`SSH_AUTH_SOCK` 指向这个 agent socket
- **工具链**：直接用 host 上的 git / node / python / cargo / 任意命令；不维护命令白名单
- **跟 Lyre 通信**：通过 Unix socket（如 `/tmp/lyre/gateway.sock`）调 Lyre 工具

唤醒结束（正常退出或被 kill），Lyre 主进程 reap 子进程、`rm -rf` tmpdir、kill task-local ssh-agent、异步撤公钥。

**跨平台**：方案纯 POSIX——Linux / macOS / WSL2 一致，无 OS-specific 沙箱依赖。

### 4.2 可选：Lyre 整体容器化（部署拓扑）

若 owner 想要 OS 级隔离（如不想让 agent 看见 `~/.aws/` 等 host 文件），**部署时把 Lyre 整体装进 Docker**——Lyre 主进程和所有 agent subprocess 共享这一个 container envelope。一份开销解决全部任务的 OS 级隔离。

- **Lyre 代码层不变**：仍是 subprocess + tmpdir，不知道自己在不在容器里
- **部署者决策**：本地开发 / 自用 → 裸跑（最快、调试最易）；要安全护栏 → docker run lyre
- 这是**部署拓扑**问题，不是**架构**问题

### 4.3 Agent 进程内 shell 自由

Agent 进程**不受命令白名单约束**。可以：

- 任意 git 操作（commit、branch、push、rebase、stash……）
- 任意 shell 操作（`bash -c "..."`、管道、重定向、子进程……）
- 任意 package manager（npm / pip / cargo……，但这些会污染 host 用户级安装目录——见 §4.5 风险）
- 任意文件操作（在 tmpdir 内）
- 任意网络访问（MVP 不做出口白名单）

理由：

- 维护命令白名单是无穷追加战；不同语言生态要求迥异，注定不完备
- 防 `bash -c "..."` 命令注入是不可能任务
- 安全靠 scoped credentials + env scoping + 可选 Lyre 整体容器化，不靠命令白名单

### 4.4 Lyre 工具走 gateway

Agent 通过 LLM function-calling 协议调用 Lyre 工具。Provider 适配器把这些 function call 转发到 Unix socket gateway。

MVP Lyre 工具集：

| 工具 | 用途 |
|---|---|
| `mailbox_send(to, body, title?, urgency?, reply_to?, forward_msg_id?, deliver_at?/in?, recur_every?/cron?)` | 写消息到任何 agent 的 mailbox；支持 broadcast、reply、forward、future-mail、recurring（见 §future-mail）|
| `mailbox_read(box="inbox"\|"sent", recipient?, include_read?)` | 默认返回**未读** listing（仅 id+sender+title+body_chars，**无 body**），并**自动写 read_at**；`box="sent"` 看自己发过的；`include_read=True` 看 archive |
| `mailbox_get_message(msg_id)` | 拉单条 mail 的完整 body（mailbox_read 只给 listing） |
| `mark_read(msg_id|msg_ids)` | 显式标 read（mailbox_read 已经会自动标，此为显式 dismiss）|
| `report_progress(state)` | 写阶段进度到 task.checkpoint；**仅用于崩溃恢复**，对 owner / 其他 agent 不可见 |
| `report_side_effect(kind, details)` | 自报已发生的外部副作用（如已 push 分支、已开 PR）；触发 Tier 1 / 2 检测 |
| `read_memory(rel_path)` | 受限只读 `~/.lyre/memory/` 下条目 body |
| `list_agents(include_archived?)` | 列出当前 agent 实例（不是 persona），用于 mailbox_send / dispatch_task 选 target |
| `list_personas()` | 列出 persona 角色定义（"我能 create_agent 哪些类型"）|
| `list_tasks(persona?, status?, limit?)` | 当前/最近的任务实例 |
| `list_models()` | 查 model registry + 健康状态 |
| `create_agent(persona, name?, model?, description?)` | 注册一个新 agent 实例；返回 agent_id（同时预创建 `~/.lyre/memory/facts/agent-<id>-notes.md` 笔记文件）|
| `archive_agent(agent_id)` | 软删（不能 archive bootstrap 的 owner / leader）|
| `dispatch_task(agent, goal, acceptance, lease_duration_s?)` | 派活给指定 agent_id；返回 task_id。派完直接停止调 tool，让 wakeup 关闭；child 完成会 mail 回来，auto-wake-on-mail 续起 |
| `query_task_status(task_id)` | 查 task 当前 status + checkpoint summary。用来轮询 child progress |
| `update_scratchpad(content, mode)` | 写 agent 自己的短期记忆文件 `memory/scratchpad/<id>.md`，append / overwrite |
| `list_scheduled_mail(recipient?, sender?, status?)` | 列 future-mail 队列 |
| `cancel_scheduled_mail(id, reason?)` | 取消未来 mail（recurring 时停止所有未来 occurrence）|
| `python_exec(code)` | 在 worktree 或主进程跑 python 代码片段 |
| `shell_exec(argv|cmd)` | 在 worktree 或主进程跑 shell 命令 |

> **说明**：每个 persona 的实际可用工具是上表的子集，由 `personas/<name>.md` 的 `allowed_lyre_tools` 决定。例如 `worker-maintainer` 没有 `dispatch_task` / `create_agent`；`owner` 不是 LLM agent 不持有任何工具。
> **read_file / write_file / git_commit / git_push 等不在此处** —— 它们都通过 `python_exec` / `shell_exec` 在 agent 进程内执行，不经 Lyre gateway。Lyre 仅作事后观测（通过 agent 自报 `report_side_effect` 或 git 状态检查）。
> **2026-06 实地复核：维持排除。**（English: file tools stay excluded — field-validated; falsifiable revival conditions below.）对一次 live 部署的全量取证（434 transcripts / 1522 次 exec 调用）显示：原地编辑仅占 exec 的 0.46%，无一例失败可归因于缺文件工具；大参数截断的真实根因是输出预算在 content-in-args 发射中途耗尽（runtime 对 `_raw` 的处理即针对此），换成 `write_file` 形状的工具不改变 content-in-args 的预算占用，救不了截断。**复活条件**——任一 tier、可观比例地观测到下列任一类时重审，引入最小文件工具组（read_file / edit_file / write_file）：① 编辑重试死循环、且 edit_file 的唯一匹配检查本可避免；② max_turns 耗尽且 turn log 以编辑为主；③ 进程被杀导致半写文件损坏。第一响应是 prompt 层指导（skill / persona 提示），引入工具是第二响应。
> **mark_processed → mark_read 重命名（migration 0005）**：旧 `mark_processed(msg_id)` 推进 cursor 的语义已被 per-message `read_at` 取代。新工具叫 `mark_read`，并且 `mailbox_read` 自动 mark，所以大多数时候根本不需要显式调用。

> **`read_file` / `write_file` / `run_command` / `git_commit` / `git_push` 等不在此处**——它们都通过 agent 进程内 shell 直接执行，不经 Lyre gateway。Lyre 仅作事后观测（通过 agent 自报或 git 状态检查）。

### 4.5 安全边界（取代命令白名单的"轮辐"）

| 防线 | 机制 | 强度 |
|---|---|---|
| **进程级 scoping** | per-task tmpdir + minimal env + task-local ssh-agent | 弱——agent 是 host 普通进程，OS 权限同 owner 用户。Agent 能 `cd ..` 跳出 tmpdir、读 host 上 owner 可读文件、`npm install -g` 污染 host |
| **Scoped credentials** | per-task ephemeral SSH key；权限取决于 git hosting | 强——git push 范围受 key 授权约束 |
| **无敏感凭据注入** | minimal env 清洗 | 中——agent 不会"恰好继承"敏感 env vars，但若 owner 把 token 写在 `~/.gitconfig` 或别处，agent 读得到 |
| **Lyre 工具 gateway** | 持久层（mailbox / global / local-hot）只能通过 Unix socket gateway 访问 | 强——agent 拿不到 Lyre 持久层连接信息 |
| **可选 Lyre 整体容器化** | docker run lyre | 强（若启用）——OS 级隔离 envelope，所有 agent subprocess 在内 |
| **Hosting 自身护栏** | branch protection / pre-receive hook | hosting-dependent |

**MVP 安全模型总评**：

- 威胁模型 = "agent 蠢" 不是 "agent 恶"。LLM 不会主动 exfiltrate owner 文件；唯一危险是 prompt injection，地基阶段任务可控
- Agent 能做的事 ≤ owner 本人能做的事——没多出新攻击面
- 出问题 owner 自己机器一眼看出来
- 想升级 OS 级隔离 → docker run lyre，**Lyre 代码层零改动**

### 4.6 Skills 工作流（Hermes / Pi 对齐）

> 详见 [`FOUNDATION.md §3.8`](./FOUNDATION.md#38-global-层的具体形态skills--soul--facts-三类条目)。

Skills 是 global 层的程序性配方（procedural recipes），存为 markdown + YAML frontmatter 文件，所有 persona 共享。

**使用流（progressive disclosure）**：

```
[Wakeup Step 5 Context 装配]
  → Lyre 从 skills 表按 (persona 兼容 ∧ scope 兼容 ∧ 相关性 top-k) 选若干
  → 只把 frontmatter（name + description + triggers + required_tools）塞进 context
  → typical：5-15 个 skill frontmatters，每条几十 token，总开销 < 1k token

[Agent 执行 loop]
  → LLM 看完 frontmatters，判断当前任务匹配哪个 skill
  → 调 load_skill("apply-dependency-upgrade") 拿到完整 body
  → 按 body 步骤干活
  → 干完后如果发现"我用了 skill X，且 X 写得不全 / 应该有但没有"，可调 propose_skill 创建或完善
```

**自荐流（self-improving loop）**：

```
[任务完成 / Step 8 准备 outputs 时]
  → Agent 判断"本次方案是否值得复用"（启发式 + persona prompt 引导）
  → 是 → 调 propose_skill(name, frontmatter, body) 创建草案，status=proposed
  → status=proposed 的 skill 进入 reviewer 队列；reviewer-persona agent 后续接审
  → 通过 → status=approved，进入 available_skills 候选池
```

**Skill body 格式约定**（v0.4 草案）：

```markdown
---
name: apply-dependency-upgrade
description: 应用一个已知正确的依赖升级（package.json + lockfile + 跑测试）
triggers: ["升级依赖", "bump version", "update package", "依赖升级"]
required_tools: [shell, git_workflow]
scope: "language:node | language:python"
version: 1
---

# 前置检查
1. 确认任务 goal 含明确的 package 与目标版本
2. 检查当前 lockfile 是否一致

# 执行步骤
1. 修改 package.json / pyproject.toml
2. 重新生成 lockfile（npm install / pip-compile）
3. ...
```

### 4.7 Owner identity（`~/.lyre/user.md`）

> 详见 [`FOUNDATION.md §3.8`](./FOUNDATION.md#38-global-层的具体形态skills--soul--memory-三类条目)。

Owner 偏好（沟通风格、技术品味、决策习惯、痛点）记录在 `~/.lyre/user.md` 这一份 user-only-writable 的 markdown 文件里。

**注入路径**：每次 wakeup，context 装配读 `~/.lyre/user.md` 整文件注入 system prompt（无 parser，无 section 解析）。文件不存在时跳过。

**更新路径**：

- **只能** owner 自己编辑 `user.md`。这是 Lyre 里唯一一类 agent 永不触碰的文件。
- Agent 想影响 owner 偏好的合法姿势：mailbox owner 提出建议，由 owner 决定要不要写进去。
- 摘要 agent 把 owner 反馈归纳成自己的 working notes（`~/.lyre/memory/facts/agent-leader-notes.md` 类），供下次 wakeup 当 context；它不写 user.md。

### 4.8 Tier 矩阵的执行

Q3 定义的 Tier 0/1/2 不再通过"Lyre 工具拦截"实现，改为：

| Tier | MVP 执行机制 |
|---|---|
| Tier 0（读、本地写、本地 commit） | Agent 进程内 shell 自由执行；无需通知 |
| Tier 1（push 分支、开 PR） | Agent 通过 git/gh shell 执行后，**调 `report_side_effect`** 自报；Lyre 收到自报后向 owner 发 `urgency=normal` mailbox 消息 |
| Tier 2（merge to main、改 CI / 依赖 / 删文件等） | Agent **应当**在 Tier 2 操作前先调 `mailbox_send(urgency=blocker)` 请示。**如果 agent 直接干**，靠 git hosting 的 branch protection 拦截（详见 §10）；拦截不住的部分依赖 persona prompt 约束 + 事后 owner 审查 |
| Tier 3（碰 secrets、跨 worktree / 跨 repo） | 进程级 scoping 提供有限保护——env 清洗 + tmpdir cwd；agent 越界访问需要它主动违反 prompt 约束。Lyre 整体容器化时可彻底拦截 |

> **MVP 不强求自动检测 Tier 2 越线**——既然 agent 自由 shell，自动检测得靠 webhook + 服务器端 hook，超出 MVP 范围。owner 接受这一层安全护栏的弱化（详见 [`FOUNDATION.md §3.3`](./FOUNDATION.md#33-铁律二lyre-与外部世界的所有交互通过-lyre-定义和实现的工具集)）。

---

## 5. 无状态约束（铁律三）

> [`FOUNDATION.md §3.4`](./FOUNDATION.md#34-铁律三拔线测试是架构对错的判据)：Agent（包括 leader 角色的 agent，无一例外）进程死亡后所有 in-memory 状态丢失。

### 5.1 硬要求

- Agent 进程不持有"下次唤醒还要用的状态"——所有跨唤醒的状态必须通过 `report_progress` 写入 local-hot
- Agent 收到 `checkpoint` 参数时，**必须**从该检查点续做，不得从头重来（除非 checkpoint 明确指示重做某一步）
- Tmpdir 在 agent 进程间**不继承**——新唤醒的 agent 拿到全新 tmpdir（旧的已 `rm -rf`）。需要保留的中间产物必须是 persistence 层条目，不能放 tmpdir

### 5.2 续做协议

详见 [`TRANSACTION_BOUNDARIES.md §7`](./TRANSACTION_BOUNDARIES.md)。

简而言之：调度器派给新 agent 的 `checkpoint` 包含——
- 最近的 commit 点对应的进度状态机
- 已 emit 但需 dedup 的消息 ID（用于幂等）
- agent 应从"checkpoint 标记的下一步"开始执行

---

## 6. Mailbox 交互（铁律五）

> [`FOUNDATION.md §3.6`](./FOUNDATION.md#36-铁律五mailbox-是-lyre-通讯的唯一原语)：所有跨 actor 通讯通过持久化 mailbox。

### 6.1 `MailboxHandle` 接口

```ts
MailboxHandle {
  send(envelope: MessageEnvelope): MessageRef
  read_inbox(box?: "inbox"|"sent", recipient?: actor_id,
             include_read?: bool): Message[]    // 返回 LISTING ONLY
  get_message(msg_id: ID): Message               // 拉单条完整 body
  mark_read(msg_ids: ID[]): void                 // 显式标 read（mailbox_read 已自动标）
}

MessageEnvelope {
  to:               actor_id | actor_id[]    // 多个就是 broadcast
  urgency:          "blocker" | "high" | "normal" | "low"
  title:            string                   // ≤140 char subject line
  body:             string
  reply_to?:        ID
  forward_msg_id?:  ID
  deliver_at?:      ISO8601                  // future-mail: 绝对时间
  deliver_in?:      "30m" | "2h" | ...       // future-mail: 相对时间
  recur_every?:     "1h" | "1d" | ...        // recurring：固定间隔
  recur_cron?:      "0 9 * * 1-5" | ...      // recurring：cron
  recur_until?:     ISO8601                  // 默认 first_fire + 1y
  metadata?:        Record<string, any>
}
```

### 6.2 提交语义

- `send` 不立即投递——它把消息放入本次唤醒的"待发"列表
- 唤醒结束的 COMMIT POINT 时，"待发"列表与持久层其它写入一起原子提交到 outbox（或带 `deliver_*` 时直接进 `scheduled_mail` 表）
- Outbox dispatcher 异步把消息从 outbox 投递到接收方的 mailbox（详见 [`TRANSACTION_BOUNDARIES.md §4`](./TRANSACTION_BOUNDARIES.md)）
- 投递保证：**至少一次**（铁律五）；接收方按 `external_id` 去重
- **`send` 完全 fire-and-forget**——不允许"send 后同步等回复"。需要等回复的语义：agent 返回 `status=needs_input`，调度器接管，待回复消息到达后重唤新 agent
- **Future-mail（0004 migration）**：`deliver_at` / `deliver_in` / `recur_*` 任一存在 → 走 `scheduled_mail` 表，scheduler 的 Phase -1 到点取出投到 outbox

### 6.3 读状态（per-message，0005 之后）

- `mailbox_read` 默认拉 `read_at IS NULL` 的 mail（按 urgency desc, id asc）
- **拉的同时立即写 `read_at = now()`**——避免 wakeup 失败后下次重读重发（idempotent，重新调不会改写 read_at）
- 想看已读 archive：`mailbox_read(include_read=True)`，**不** 自动标
- 想看自己发过什么：`mailbox_read(box="sent")`，按 id desc，**不** 自动标
- 显式标 read 用 `mark_read(msg_ids)`，主要用于 dismiss FYI 不想 reply 的 mail
- **不再有全局 cursor**——0005 之前的 `last_processed_msg_id` 字段已删；scheduler 的 Phase 0 防 auto-wake 重发用的 `last_auto_triggered_msg_id` 跟 agent 的 read state 完全独立，存在 `mailboxes.metadata` JSON 里

---

## 7. Provider 适配（铁律一）

> [`FOUNDATION.md §3.2`](./FOUNDATION.md#32-铁律一provider-中立)：Agent 后端可替换，靠这层接口。

每个 provider 需要一个**适配器**，把 provider 的执行模型映射到本契约。

| Provider | 适配要点 |
|---|---|
| **Claude Code** | 把 Claude Code 自带的工具映射：Read/Write/Bash 等 file/shell 类工具直接在 agent subprocess 内执行；Lyre 工具（mailbox_send 等）通过 Unix socket gateway 调用。适配器要把 Claude Code 的工具调用拆分到这两路 |
| **OpenAI / Anthropic API（自写 loop）** | function-calling 直接映射到 Lyre 工具 schema；shell 调用通过 subprocess exec；适配器较薄 |
| **LiteLLM / OpenRouter 网关** | 统一接口接所有 provider，把"哪个 persona 用哪个模型"变成纯配置 |
| **本地开源模型** | 同 LiteLLM 网关；要求模型支持 function-calling 协议 |

适配器责任：
- 把 `AgentInput` 翻译成 provider 期望的请求形式
- 在 agent subprocess 内运行 LLM loop；把 LLM 的工具调用分流到 shell 或 Lyre gateway
- 收集 provider 的输出，构造 `AgentOutput`
- 计量 token / 时间，填 `MeteringResult`

Provider 选择由 leader-persona agent 在 owner 制定的路由策略内决定。

---

## 8. 失败与重试

| 失败类型 | Agent 行为 |
|---|---|
| Shell 命令失败 | Agent 自行处理（在 subprocess 内观察 exit code、重试或换策略） |
| Lyre 工具调用失败（gateway 错误）| 工具返回结构化 error；agent 可重试或换策略 |
| Agent loop 失败（模型超时、解析错误） | 适配器抛错；调度器决定唤醒新 agent 续做还是计 failure |
| 跨存储 commit 失败 | 整个事务回滚；本次唤醒等于没做；checkpoint 不推进 |
| 同一任务连续失败 N=3 次 | 调度器 / leader-persona agent 检测，写 leader / owner mailbox `urgency=blocker`（铁律五 Stop trigger） |

**Agent 自己不做长期重试**——单次唤醒内可以重试个别调用，但跨唤醒的重试是**调度器 / leader 的职责**。Agent 是 stateless 求值器，不持长期重试预算。

---

## 9. 度量

```ts
MeteringResult {
  token_input:     int                       // 累计 prompt tokens
  token_output:    int                       // 累计 completion tokens
  wall_clock_ms:   int                       // 本次唤醒时长
  tool_call_count: int                       // Lyre 工具调用次数（不计 subprocess 内 shell 命令）
  provider:        string                    // 例如 "anthropic" / "openai" / "local"
  model:           string                    // 例如 "claude-sonnet-4-6"
}
```

MVP 阶段：**仅作记录**，不作判定（预算控制不在 MVP 阶段）。下一期引入预算时，从这里取数。

---

## 10. Git hosting 与 hosting-specific 行为

> **MVP 决议**：Lyre 基线**只**要求 **git over SSH**。Hosting 差异通过 persona / context 自然语言注入处理，**Lyre 代码层不做 hosting 适配器**。

### 10.1 基线：git over SSH

任何能通过 SSH 接受 git 推拉的服务器都满足 Lyre 要求。Container 内的 git 通过预先注册的 per-task SSH key 访问。

不预设：

- 没有 PR / MR 概念
- 没有 webhook 概念
- 没有 fine-grained API token 概念
- 没有 branch protection 概念

### 10.2 Hosting-specific 行为靠 persona 注入

任务派发时，leader（或 owner）在 `PersonaSpec.system_prompt` 或 `AssembledContext.task_state` 里注入对该 hosting 的自然语言说明。例如：

> "这个仓库托管在 GitHub。开 PR 用 `gh pr create --base main --head $BRANCH --title ... --body ...`。Branch protection 已经在 main 上配好——你不可能 force push 到 main。完成后调 `report_side_effect("opened_pr", { url: ... })` 让 Lyre 知道。"

或：

> "这个仓库托管在公司自建的 Gitea。开 PR 用 `tea pr create ...`，token 已在 `$GITEA_TOKEN`。"

或：

> "这是裸 git over SSH。没有 PR 概念。请把 patch 输出成文件，路径写到 artifacts，并调 `mailbox_send(to=owner, urgency=high, body=...)` 让 owner 手动 apply。"

Agent 凭一般智能读懂指令并执行。

### 10.3 安全护栏 degrade gracefully

Hosting 提供多少安全护栏，Lyre 就用多少：

| Hosting 等级 | 实际保护 |
|---|---|
| GitHub / GitLab / Gitea（有 branch protection + 细粒度 token） | 完整：scoped token + protected `main` + webhook 检测 push/PR |
| 自托管 git + SSH key + 服务器端 pre-receive hook | 完整：服务器端拒绝违规 push |
| 裸 git + SSH key | **degrades**：靠 persona prompt 约束（"永不直接 push master"）+ owner 事后审查。若 agent 不听话直接 push master，Lyre 拦不住 |

Owner 选 hosting 时知情这一权衡。MVP 阶段可选裸 git 是有意的——简单起步、信任建立、再升级 hosting 能力。

### 10.4 Lyre 代码层不识别 hosting

这是关键设计选择：**Lyre 代码不区分 GitHub / GitLab / 其它**。所有"GitHub 特殊"的事情都在 persona prompt 里，由 LLM 用自然语言理解执行。理由：

- 不增加代码维护负担（每加一个 hosting 不用改 Lyre）
- 与"agent 靠 prompt 区分角色"哲学一致——hosting 也靠 prompt 区分
- 未来出现新 hosting 时，加一段 persona 即可

代价：

- LLM 出错时（如把 GitHub 命令误用到 GitLab）没有代码层兜底
- 性能上略有损耗（每次 agent 要在 context 里读 hosting 说明）

权衡可接受。

---

## 11. 已识别但待解决的问题

> 起草 / 修订过程中浮现的子问题。

1. **Persona spec 的具体 schema**：`role_description` 与 `system_prompt` 字段够不够？是否需要"该 persona 允许调哪些 Lyre 工具"的白名单？v0.3 倾向后者——加 `allowed_lyre_tools: ToolName[]` 字段
2. ~~**Container 镜像管理**~~ → 取消（v0.3 不用 per-agent 容器；agent 是 Lyre subprocess）
3. **Lyre gateway 协议细节**：v0.3 已决 Unix socket + line-delimited JSON-RPC；具体方法签名、错误码、认证策略（同 host owner 用户 → 文件系统权限即认证）待细化
4. ~~**Container 与 agent loop 的生命周期**~~ → 取消（subprocess 模型一次 fork 跑完整唤醒）
5. **`report_side_effect` 的种类清单**：MVP 至少需 `pushed_branch`、`opened_pr`、`created_issue`——其它？
6. **Agent 在不同 persona 下的工作目录差异**：leader-persona 需要 worktree 吗？v0.3 倾向 leader 不开 tmpdir（它不动代码），但仍按 subprocess + minimal env 启动，机制一致
7. **跨 persona 的 task 派发**：leader-persona agent 通过哪个 Lyre 工具派任务给 worker-persona agent？需要 `dispatch_task` 工具
8. **Webhook 接入（未来）**：当用户接入有 webhook 能力的 hosting 时，Lyre gateway 如何接收 webhook 并转换成 mailbox 消息？
9. **Lyre 整体容器化的部署文档**：什么时候推荐 owner 启用 `docker run lyre`？需要的 mount 路径、env 注入、port forwarding 等 ops doc
10. **Tmpdir 路径与权限策略**：`/tmp/lyre/task-*` 的清理 / 防别的 host 进程窥探（`chmod 700`）/ tmpfs vs 普通 disk 的权衡
11. **Skill 检索算法**：context 装配时按"persona 兼容 ∧ scope 兼容"过滤 skills，索引中只列 frontmatter；agent 按需 `load_skill` 加载 body。没有 embedding 相似度（global 层不再有向量检索）。
12. **Skill 审核流**：`status=proposed` → `approved` 的审核者是 leader-persona、专门 reviewer-persona、还是 owner 手工？v0.4 倾向 reviewer-persona 自动 + owner 偶尔 spot-check；reviewer 是新 persona
13. ~~user.md 更新触发的去抖机制~~ — 不适用，agent 不写 user.md。
14. **Skill body 模板规范**：v0.4 给了简单 markdown 模板，但前置检查 / 执行步骤 / 失败处理 / 验证标准等结构化 section 是否要统一约定？倾向松约束（agent 写得清楚就行），但模板里给推荐结构

---

