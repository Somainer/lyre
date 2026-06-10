# Lyre — 架构奠基文档

> **文档定位**：Lyre 架构的稳定参照。"架构内核"小节（铁律一至五、三档持久层、三类 global 条目）为**定论**（status: settled），其它设计文档以本文为依据。
>
> **Status note (2026-06-10)**: 架构内核的**五条铁律本身仍为定论**。但 §3.3 的 enforcement model（subprocess sandbox + Unix-socket gateway）与 §3.7 的五层架构表描述的是 v0.x 计划，不是建成的运行时——see the correction note in §3.3. For the as-built runtime read `RUNTIME_CURRENT.md` (living doc).

---

## 目录

1. [Lyre 是什么](#1-lyre-是什么)
2. [项目由来（设计取向的解释性背景）](#2-项目由来设计取向的解释性背景)
3. [架构内核（定论）](#3-架构内核定论)
   - 3.1 [控制链（默认 persona 路由）](#31-控制链默认-persona-路由)
   - 3.2 [铁律一：Provider 中立](#32-铁律一provider-中立)
   - 3.3 [铁律二：Lyre 与外部世界的所有交互通过 Lyre 定义和实现的工具集](#33-铁律二lyre-与外部世界的所有交互通过-lyre-定义和实现的工具集)
   - 3.4 [铁律三：拔线测试是架构对错的判据](#34-铁律三拔线测试是架构对错的判据)
   - 3.5 [铁律四：持久层按作用域分三档](#35-铁律四持久层按作用域分三档)
   - 3.6 [铁律五：Mailbox 是 Lyre 通讯的唯一原语](#36-铁律五mailbox-是-lyre-通讯的唯一原语)
   - 3.7 [五层架构（整体分层）](#37-五层架构整体分层)
   - 3.8 [Global 层的具体形态：Skills / User / Memory 三类条目](#38-global-层的具体形态skills--soul--memory-三类条目)
4. [工程后果（拔线测试的三条硬约束）](#4-工程后果拔线测试的三条硬约束)
5. [开源现状结论（避免重复造轮子）](#5-开源现状结论避免重复造轮子)
6. [路线（第一步建议）](#6-路线第一步建议)
7. [文档导航](#7-文档导航)

---

## 1. Lyre 是什么

Lyre 是一个长期运转的个人多 agent 团队基础设施。目标**不是**"一次性完成某个任务"，而是构建一个能持续数月乃至数年、自治协作、为单一所有者长期推进真实工作的 agent 组织。

**命名取意**：里拉琴——多根弦张在一个固定的框架上，弦可拨可换，框架不动；多弦共奏成和声。这精确对应 Lyre 的架构内核：固定的持久层骨架 + 可替换的活动 agent 单元 + 多 agent 协作。"Lyre" 一名已通过查重（AI / agent / 编程语言领域无重名冲突），由所有者于 2026-05-16 确认沿用为正式项目名。

**首个落地场景**：持续维护和演进所有者的真实代码项目——长期看护一批真实代码库，处理演进、重构、bug 修复、依赖更新等。

**第一版优先级**：先把架构地基打扎实，**不急于跑通最小可用版本**。理由：让 agent 长期操作真实代码库，鲁棒性不达标即是灾难。开局应追求"拔线测试能过"的不可见地基，而非"能维护代码"的可见成果。

---

## 2. 项目由来（设计取向的解释性背景）

> 这一节解释 Lyre 某些设计为何看起来"过度"。它不是定论，但理解它有助于不被表面冗余迷惑。

Lyre 最初从一个完全不同的设定推演出来：设计一个"赛博奇观"——在 AI 烧 token 军备竞赛背景下，让 agent 烧海量 token 做类似比特币 PoW 的工作量，每个 token 都用在真实可验证的创作上，整体如古人造金字塔般宏伟而无外部用途。

把这个奇观方案做扎实的过程中，逐步意识到：奇观只是压力测试，真正有价值的产物是被它逼出来的架构骨架——一个能长时间自治运转的多 agent 组织。于是项目转向"做有用的事"。**架构一行不用改**，改变的只是装进去的任务。

转向"有用"后的三个重心变化，必须贯彻：

1. 评价标准从"烧得多、单调累积、可展览"变为"产出有价值、可信、可用"。难度递增（PoW 那套机制）从卖点降级为**需要被控制的成本**——现在追求高效，不是越跑越贵。
2. **人在环里的位置从可选变为核心**。所有者与 leader 的对接、自治边界、撞线即停，是核心机制，不是附加功能。
3. 早期纯为奇观服务的机制（虚构语言生态、内容正典化、persona 世代演化、灾变事件等）不是 Lyre 的需求，可忽略。

---

## 3. 架构内核（定论）

> **status: settled** ——本节以下所有规则为 Lyre 的奠基条款，后续工作照此推进。

### 3.1 控制链（默认 persona 路由）

Lyre 中**没有"Leader 进程"和"Worker 进程"两种实体**——只有 **Agent** 一种执行实体（详见 [`AGENT_CONTRACT.md`](./AGENT_CONTRACT.md) §1）。Agent 之间靠 **persona** 区分角色。`leader`、`worker-maintainer`、`reviewer` 等都是 persona spec，不是不同的进程类型。

默认 persona 路由：

- **所有者（owner）**：背后有人的 actor。下达高层意图、制定规则、审批 leader-persona agent 标记为"需拍板"的事项。**不微操，不审常规产出**。
- **`leader` persona 的 agent**：自治调度。把所有者意图拆解成任务、按依赖关系 dispatch 给 worker-persona agent、为每个任务挑选 provider/模型、收取产出并决定采纳/打回、定期向所有者汇报、撞规则边界时停下请示。Leader persona 的 agent 工具是**调度类工具**（查依赖图、查 agent 状态、派任务、收回报、向所有者汇报）；**不写代码、不进 worktree**。
- **`worker-*` persona 的 agent**（如 `worker-maintainer`、`worker-reviewer`、`worker-test-planner`）：在 leader 给定的 task + context + 工具集内完成单个任务，回报产出与 token 消耗。

**这是默认 persona 路由模式，不是机制约束**——机制层面（[铁律五 §3.6](#36-铁律五mailbox-是-lyre-通讯的唯一原语)）任何 agent 可以写任何其它 actor 的 mailbox。Leader-persona agent 可授权 worker-persona agent 越级写 owner（如安全敏感事件）、可批准若干 worker-persona agent 组成临时小组互通——皆为策略授权。但默认情况下：worker-persona 只跟 leader-persona 说话；owner 只跟 leader-persona 说话；leader-persona 是 owner 与底层的唯一接口。

**未来扩展不需要造新抽象**：要加"安全审计"persona、"测试规划"persona、"子 leader"persona ——都是新 persona spec，机制层 agent 一种就够。

类比：所有者是君主，leader-persona agent 是宰相，worker-persona agent 是工匠。所有人都是同一种"臣民"（agent），只是君主授予了不同的官职（persona）。君主不亲自搬石头，也不把国家交给机器自转——但宰相可以授权工匠在特殊情况下越级请见，也可以让若干工匠合议某项工程。

### 3.2 铁律一：Provider 中立

不假设 agent 只能是某一家厂商（如 Claude）。Lyre 与任何具体 agent 之间隔一层薄的 `Agent` 抽象接口（详见 [`AGENT_CONTRACT.md`](./AGENT_CONTRACT.md)）：

| 维度 | 契约 |
|---|---|
| 输入 | persona 定义 + task + 组装好的 context + 允许使用的工具集 |
| 输出 | 产出物 + 产生的事件 + token / 成本计量 |
| 硬要求 | **无状态** |

任何能满足此契约的后端都可作为 agent：自写的 agentic loop、Claude Code、开源模型、本地模型。建议在中间放一个 **LLM 网关**（LiteLLM / OpenRouter 类），用统一接口接所有 provider，使"哪个 persona 用哪个模型"成为纯配置。

**Provider 选择权的归属**：路由规则由**所有者**制定，执行由 **leader** 在规则内自由决定。例如所有者给出策略"维护者 persona 用强模型、reviewer 用中等模型、预算紧张时降级到开源模型"，leader 在此策略内自主路由。

**关于 Claude Code 的 agent view / agent teams**：它们是 agent 层的一种可选后端 + 监督 UI，**不是 Lyre 的地基**。Lyre 不基于它们构建。可以接入，但只是众多 agent 后端之一。

### 3.3 铁律二：Lyre 与外部世界的所有交互通过 Lyre 定义和实现的工具集

> **实现修正（2026-06-10）**: The law stands; this section's *enforcement model* is the v0.x plan and was **not built**. As-built: agents run **in-process** in the scheduler (or as one `lyre run-task` subprocess per wakeup). A per-task scratch worktree and (for `git_context` tasks) an ephemeral SSH key do exist, but as working artifacts only — they are **not** the env-scoped fork sandbox this section describes; the Unix-socket tool gateway was never built (`src/lyre/mcp_server/` is an empty stub). `shell_exec` / `python_exec` **are** Lyre tools dispatched in-process by the wakeup loop — so the law holds in the "every action goes through a Lyre tool" sense (CLAUDE.md's wording), not via this section's subprocess/gateway split. The table's "强" rows for the gateway are superseded: real containment = the shell/python env allowlist (strips `ANTHROPIC_*` / `LYRE_*`; **deliberately forwards `GH_TOKEN` / `GITHUB_TOKEN`** as worker capability — the "agent 不继承 GH_TOKEN" row below is wrong, `src/lyre/runtime/shell.py`) + the single-owner trust model (`CAPABILITY_DISCOVERY.md` §3). A gateway-like seam may be revived via the parked plugin spec (`PLUGINS.md`) if endorsed.

Agent 是 Lyre 派生的 subprocess，跑在 per-task tmpdir + minimal env + task-local ssh-agent 内。Agent **可以**在 subprocess 内拥有任意 shell——这无所谓，因为 tmpdir / env / ssh 都是 scoped 的，agent 跟外部世界的接触面被严格控制。

**Subprocess 内自由**：Agent 可任意调用 shell、git、语言工具链、网络访问（MVP 不做出口白名单）。维护命令白名单是无穷追加战；不同语言生态注定不完备；scoped credential 模型下命令白名单的安全收益小于复杂度。

**外部世界接触面控制**（取代命令白名单的"轮辐"）：

| 防线 | 机制 | 强度 |
|---|---|---|
| **进程级 scoping** | per-task tmpdir + minimal env + task-local ssh-agent；subprocess 跟 Lyre 同 OS 用户 | 弱——agent 是 host 普通进程；OS 权限同 owner。Agent 能 `cd ..` 跳出 tmpdir、读 host 上 owner 可读文件 |
| **Scoped credentials** | per-task ephemeral SSH key；权限取决于 git hosting；MVP 基线 = git over SSH（详见 [`AGENT_CONTRACT.md §10`](./AGENT_CONTRACT.md#10-git-hosting-与-hosting-specific-行为)） | 强——git push 范围受 key 授权约束 |
| **无敏感凭据注入** | minimal env 清洗，agent 不继承 LYRE_* / GH_TOKEN / API key 等 | 中——env 干净，但 agent 可读 owner 的 `~/.gitconfig`、`~/.aws/` 等若 owner 没调权限 |
| **Lyre 工具 gateway** | 持久层（mailbox / global / local-hot）只能通过 Unix socket gateway 访问；agent 拿不到持久层连接 | 强 |
| **可选 Lyre 整体容器化** | `docker run lyre`——Lyre 主进程与所有 agent subprocess 共享一个 container envelope；OS 级隔离的可选层 | 强（若启用） |
| **Hosting 自身护栏** | Branch protection / pre-receive hook 等由 git hosting 提供；hosting 越强 Lyre 安全越强；hosting 弱时降级到 persona prompt 约束 + 事后审查（degrade gracefully） | hosting-dependent |

**Lyre 工具集** 严格仅承担**外部世界接触面**的事情：mailbox / artifact 落盘 / 持久层访问 / 自报副作用 / 派任务 / 度量。Subprocess 内的 file I/O、shell、git 操作**不**走 Lyre 工具，agent 直接用 shell。

理由（保留原 3 条精神）：

- **安全可审计**：所有"会影响 Lyre 持久层 / 跨 actor 状态"的副作用过 Lyre 工具
- **公平竞争**：不同 provider 在 Lyre 工具接口层一致（subprocess 内自由不影响这层比较）
- **生态规则统一**：PR / review / mailbox 等仍是 Lyre 法律，由 Lyre 工具 + persona prompt 共同实施

**代价**：

- Tier 1 / Tier 2 越线检测不再靠工具层拦截，靠 agent 自报 + git hosting 自身护栏；hosting 能力弱时安全性 degrade gracefully
- 进程级 scoping 弱于容器隔离——agent 出 tmpdir 是可能的；威胁模型限定为"agent 蠢"而非"agent 恶"；owner 想要 OS 级护栏可启用整体 Lyre 容器化
- 适配器要把 provider 的工具调用拆分为"走 subprocess shell"与"走 Lyre gateway"两路（如 Claude Code 适配较厚）

**这些代价值得承担**——换来跨平台一致（Linux / macOS / WSL）、零容器开销、调试简单，且 OS 级隔离作为部署期可选项保留。

### 3.4 铁律三：拔线测试是架构对错的判据

任何 agent（含 leader、含未来的子 leader，**无一例外**）的对话上下文都是**纯缓存，不是真相**。可随时丢弃，丢弃后能从持久层完整重建。

**检验标准——拔线测试**：在任意时刻 kill 任意进程，系统不丢失任何已提交的工作，重启后能精确恢复。**任何过不了拔线测试的设计，判错。**

推论：

- **Agent = 对持久状态的一次短暂求值**，不是常驻进程。Agent 不是常驻服务，是"持久层里的档案 + 一次唤醒"。一个 agent 进程死一百次，对应的 persona 与任务进度毫发无伤。
- **任务属于持久层，不属于任何 agent 进程**。任务的执行进度是一等持久对象，可断点续传。Agent 中途死亡，接手者（另一个 agent 进程，可同 persona 或不同）从持久层读取进度无缝接续。
- **任务应可切分为带提交点的步骤**，使拔线损失最小化（做完的提交点算数，未完成的那一步作废重来）。
- **任何 persona 都是角色不是实体**——"当前 leader"、"当前 reviewer"、"当前 maintainer" 都只是被唤醒来扮演该角色的一次求值。CEO 换人，公司不重启，因为公司状态在公司里，不在 CEO 脑中。Leader-persona、worker-persona、未来任意 persona 都适用此规则。

**副产物**：全状态落盘 ⇒ 生态演化史可完整回放、可审计；agent 无状态 ⇒ 可水平扩展到大量并发 agent；provider 可在任务中途热切换（接手 agent 读持久层，不依赖前一个 agent 的对话历史）。

工程后果详见[第 4 节](#4-工程后果拔线测试的三条硬约束)。

### 3.5 铁律四：持久层按作用域分三档

**分层依据是作用域，不是内容大小**。判据：

> "这个状态，除了产生它的那次任务，还有谁需要它？没有别人需要 → local；有 → global。"

效率是这个原则的**结果**，不是原因。

| 档位 | 内容 | 绑定对象 | 可见性 | 生命周期 |
|---|---|---|---|---|
| **Local-hot** | 任务私有的执行状态：进度状态机、中间推理、试错记录、临时草稿、本次唤醒读过的文件清单 | 任务（**不绑定具体 agent 进程**，因为 agent 无状态会换人） | 任务私有，生态内其他 agent 不可见 | 参与拔线恢复；任务完成或废弃即清空 |
| **Global** | 生态公共事实：merge 进 git 的代码与文档、PR / review 的最终结论、依赖图、引用图、persona 长期档案、排名 / 统计数据 | 生态 | 全生态可见 | 单调累积、精炼；只存"结论密度"，不存"过程密度" |
| **Cold-archive** | 已完成任务的完整过程留存 | 任务（已封存） | 按指针精确取用 | 海量、只读；不影响在线效率；不删除（审计 / 复盘 / 研究 agent 行为仍有价值） |

**Global 层的写入路径**：

- **User** identity（`~/.lyre/user.md`）由 owner 自己 ad-hoc 编辑。`lyre onboard` 写出初始模板；之后 owner 改动 → 下次 wakeup 自然生效。
- **Skills**（`~/.lyre/skills/`）由 agent 自荐：`propose_skill` 写到 `proposed/` → reviewer-skill 审定 → 移到 `approved/`。
- **Memory / facts**（`~/.lyre/memory/`）由 agent 直接 `shell_exec` / `python_exec` 写文件，没有审批。索引按文件名 + frontmatter，检索靠 grep。

**Context assembly 的结构**：

绝大多数 agent 唤醒只需 local-hot + global；cold-archive **仅在需要追溯时按指针精确取，绝不全量扫描**。一次唤醒的 context 组装顺序：

1. Owner identity（`~/.lyre/user.md`，global，整文件注入 system prompt）
2. Persona system_prompt（global，shipped + user override）
3. 当前任务的执行进度与现场（local-hot，断点续传靠此）
4. 相关 skills frontmatter + memory facts 索引（global）；用得上时 `load_skill` / `read_memory` 加载 body
5. 极少数情况下按指针精确取的历史过程片段（cold-archive）

### 3.6 铁律五：Mailbox 是 Lyre 通讯的唯一原语

每个 actor 角色（owner、以及每种 persona 角色——leader、worker-maintainer、reviewer、未来任意新 persona）有且仅有一个 mailbox。**所有跨 actor 通讯均通过 mailbox**——不存在 in-process 共享状态、不存在"直接调用某个 agent"、不存在内部 RPC 走旁路。

Owner 不是系统外的用户，是"背后有人的 actor"，与其它 actor **同等地位**。系统对 owner 的待遇与对 leader 完全一致：持久化 mailbox + stateless 唤醒（owner 这个人不在线时 mailbox 照常堆消息，TA 上线就是读 mailbox）。

**消息 schema（唯一）**：

```
{
  from:           actor_id,
  to:             actor_id,
  urgency:        blocker | high | normal | low,
  body:           自由文本,
  task_id?:       可选关联任务,
  parent_msg_id?: 可选 —— 回复哪条,
  timestamp:      time,
  metadata?:      可选 kv（仅系统生成消息打标用，协议不强制）
}
```

**没有 `type`**。两端都是智能体（LLM 或人），自然语言足够表达。`type` 是为大流量预设统计与路由优化预留的设计，地基阶段是负债不是资产。统计需求出现时由 LLM 临时分类。

**修订：系统元数据协议（2026-06-10，owner 决议）**："没有 `type`"条款管的是 **agent↔agent 语义层，仍然成立**——智能体之间不引入消息类型系统，自然语言足够。与此同时，邮件与任务 `metadata` 下的 `kind`、`fan_in`（含 `fan_in.group_id` / `leg_key` / `result`）、`thread_id`、`auto_dispatched`、`broadcast_id` 等键是 **runtime 保留命名空间**：仅由 runtime / 工具层写入，仅系统生成的邮件依赖它们路由（fan-in barrier 计数、thread 历史、supervision / `task_terminated` 标记、auto-wake 抑制）。Agent 永远不需要自己读写这些键——需要时通过工具参数（如 `mailbox_send(result_for=…, thread_id=…)`）由工具层代为打标。实现零改动，此为对既成事实的正典承认与划界。

**Urgency 四档**：

| 档位 | 语义 |
|---|---|
| `blocker` | 系统在等你回复，不回则任务停滞 |
| `high` | 最好回，不回也不致命 |
| `normal` | FYI，可不读不回 |
| `low` | 存档用，UI 默认折叠 |

**Inbox / Dashboard 不是两个通道，是同一 mailbox 的两种视图**：

- Inbox 视图 = `urgency >= high`（必看必处理）
- Dashboard 视图 = 全量（含 normal / low）

**拓扑机制层面全开放**：任何 actor 可以写任何其它 actor 的 mailbox。控制链（[§3.1](#31-控制链默认-persona-路由)）是**默认路由策略**：由 leader-persona 或全局策略管理；机制不强制。Leader-persona agent 可授权 worker-persona agent 越级写 owner（如安全敏感事件）、可批准若干 worker-persona agent 组成临时小组互通——皆为策略授权范畴。

**消息约束**（与 [§4 事件总线约束](#4-工程后果拔线测试的三条硬约束) 完全一致——mailbox 即事件总线在 actor 边界上的特化）：

- 持久
- 可重放
- 至少一次投递
- 消费方幂等

**Stop triggers 是条件，不是消息类型**。下列条件被基础设施检测到时，向相关 mailbox 写入一条 `urgency=blocker` 的自然语言消息（可在 `metadata` 里附 kv 供未来统计）：

| 条件 | 初始参数 |
|---|---|
| 同一任务连续失败 N 次 | N=3（可配） |
| 撞自治边界 | — |
| Leader 自评不确定性超阈 | — |
| 外部资源不可达超过 X 分钟 | X=10（可配） |
| 触发硬白名单之外的安全 / 隐私敏感操作 | 白名单待 MVP 中后期定义 |

> **预算控制不在 MVP 阶段**。MVP 阶段无预算超阈触发器、无 cost-stream 仪表板、无 budget-warn 升级机制。

### 3.7 五层架构（整体分层）

| 层 | 组成 | 备注 |
|---|---|---|
| **执行环境** | Per-task tmpdir + Lyre 派生 subprocess + task-local ssh-agent；跨 unix-like 平台一致 | 可选部署：整个 Lyre 装进 Docker container 作 OS 级 envelope（详见 [`AGENT_CONTRACT.md §4`](./AGENT_CONTRACT.md#4-运行环境与工具集铁律二)） |
| **记忆 / 持久层** | git（代码 / 文档真相源）+ SQLite（runtime state：mailbox / outbox / tasks / wakeups / agents）+ 文件系统（global = user.md / personas / skills / memory / config）+ 对象存储（冷归档、调用日志）。Scale 时 SQLite → Postgres，对象存储 → S3 兼容。**没有向量库**——facts 是普通 markdown，由 agent 用 grep / shell_exec 自维护。 | [§3.5](#35-铁律四持久层按作用域分三档) 三档持久层的物理实现；详见 [`PERSISTENCE_SCHEMA.md`](./PERSISTENCE_SCHEMA.md) |
| **Agent Runtime** | ① persona + context assembly + 工具定义（**provider 无关，核心资产**）<br>② Agent 后端（**可插拔**） | 这两半的边界即 [§3.2](#32-铁律一provider-中立) 的 `Agent` 接口（详见 [`AGENT_CONTRACT.md`](./AGENT_CONTRACT.md)） |
| **编排** | 事件总线 + 调度器 + 工作流引擎（建议 Temporal：长时运行、可重试、可重放、可视化） | Leader-persona agent 处于此层。Mailbox（[§3.6](#36-铁律五mailbox-是-lyre-通讯的唯一原语)）是事件总线在 actor 边界上的特化 |
| **观测** | 给所有者的监督面板：进度、待拍板事项、成本 | 监督对象是**调度器和生态本身**，不是一堆孤立会话 |

### 3.8 Global 层的具体形态：Skills / User / Memory 三类条目

> [§3.5 铁律四](#35-铁律四持久层按作用域分三档) 定义了 global 层是"生态公共事实"。本节给出 global 层的**三种具体条目形态**及其 authorship 边界。**Global 层在物理上统一是文件系统**（`~/.lyre/` 下的 markdown + 目录）；没有向量库，没有 promotion 流水线（除了 skills）。

#### Global 层的三种条目形态

| 形态 | 物理位置 | 谁写 | 谁读 |
|---|---|---|---|
| **User**（owner identity & 偏好）| `~/.lyre/user.md` | **仅 owner**（用户本人）| 所有 agent 装配 context 时直接注入整段（无 parser）|
| **Skills**（程序性配方）| `~/.lyre/skills/approved/<name>/SKILL.md` | Agent 通过 `propose_skill` 提交 → reviewer-skill 审批后晋升 | Agent 看 frontmatter 索引；用得上调 `load_skill` 加载 body |
| **Memory / Facts**（agent 自维护的事实笔记）| `~/.lyre/memory/facts/<topic>.md` | **仅 agent**（约定上 owner 不动） | Agent 用 grep / shell_exec / read_memory 按需检索 |

**Authorship 边界（约定，非技术强制）**：
- User：owner 写，agent 读不写。Agent 想影响 owner 偏好的正确姿势是 mailbox owner 建议，让 owner 自己改 user.md。
- Memory：agent 写，owner 读不改。这保证 agent 的 working notes 不会被人手抖打乱。

#### Skills 的晋升通道（global 层唯一的 promotion 机制）

Skills 是 know-how，需要质量把关：agent 干完任务判断"值得复用" → `propose_skill` 写到 `skills/proposed/` → reviewer-skill persona 审定 → 文件移到 `skills/approved/`。这是 global 层唯一保留的 propose-review 流水线。

**Facts 没有 promotion**：早期方案把"事实陈述"作为单独 promotion 通道（数据库 + 向量检索），后来认定 AI 用 grep / shell 自管理 markdown 文件已经够好——hand-rolling 向量检索容易错、效果不一定更好。所以 facts 退化为"agent 想写就写"的 memory 文件，索引只用文件名 + frontmatter。

详细 schema 见 [`PERSISTENCE_SCHEMA.md`](./PERSISTENCE_SCHEMA.md)；agent 侧使用见 [`AGENT_CONTRACT.md`](./AGENT_CONTRACT.md) §4。

---

## 4. 工程后果（拔线测试的三条硬约束）

铁律三对工程实现派生三条硬约束，**必须认真对待**：

1. **跨存储写入必须事务性原子**。一次 agent 动作可能同时要写 git、写数据库、更新依赖图、推事件——要么全成要么全不成。**半成功是最毒的状态**（拔线拔在半成功上，系统进入无法重建的不一致态）。需要 **outbox 模式** 或 **saga 类机制** 保证最终一致。
2. **事件总线必须持久化、可重放、至少一次投递、消费方幂等**。丢事件会破坏拔线测试。
3. **所有 agent 动作必须幂等**。拔线-重启意味着任何操作可能被执行两次，重复执行不得产生重复后果（例如"提了 PR 但没记下来，重启又提一遍"必须能识别"此 PR 已存在"）。

---

## 5. 开源现状结论（避免重复造轮子）

调研结论：**没有现成的"开箱即用的 Lyre"**，但每个零件都有开源。

**可借鉴 / 复用**：

| 领域 | 候选 | 备注 |
|---|---|---|
| 多 agent 编排 | MetaGPT、ChatDev、AutoGen | 都是**任务级、非长期** |
| 记忆层 | Mem0、Letta / MemGPT、MemOS 的分层记忆、Cognee | |
| 工作流引擎 | Temporal | 长时运行、可重试、可重放、可视化 |
| 持续群体模拟的架构思路 | Stanford generative agents / Smallville | 记忆流与反思机制 |

**真空地带**（即 Lyre 的核心工作量，需自己构建）：

- 长时运行的协作生态
- 跨任务的依赖关系与级联响应
- 统一的 context assembly 方法论
- 多 agent 审稿 / 评审机制

---

## 6. 路线（第一步建议）

既然优先级是"先把地基打扎实"，开局顺序：

1. ~~**先把 `Agent` 接口契约写死**~~——精确定义字段：persona 如何传入、context 如何传入、artifacts 与 events 如何回传、token 如何计量、mailbox 读写如何暴露。✅ v0.2 已落地于 [`AGENT_CONTRACT.md`](./AGENT_CONTRACT.md)。
2. ~~**再设计"一次 agent 动作的事务边界"**~~——一次 agent 唤醒从读 mailbox + 持久层、干活、写回（含写其它 actor 的 mailbox）的精确步骤、哪个点是 commit 点、拔线拔在每个点上分别会发生什么、如何保证幂等。✅ v0.2 已落地于 [`TRANSACTION_BOUNDARIES.md`](./TRANSACTION_BOUNDARIES.md)。
3. **然后才搭最小的三档持久层 + mailbox 实现 + 一个能跑的 agent**。这是 v0.3 / 实现阶段的重点。

> **不要先追求"能维护代码"的可见成果，先追求"拔线测试能过"的不可见地基。**

---

## 7. 文档导航

> 状态标注：**定论** = settled canon；**部分历史** = 核心结论成立但含 v0.x 已被实现取代的章节（见各文件横幅）；**已落地** = PR-round changelog，描述已合并的实现；**living** = 持续更新；**parked** = 已拍板搁置。

| 文档 | 作用 |
|---|---|
| [`../../README.md`](../../README.md) | 项目入口 |
| `FOUNDATION.md`（本文件） | 架构内核五铁律，**定论**（§3.3 enforcement / §3.7 分层表为 v0.x 计划，见横幅） |
| `RUNTIME_CURRENT.md` | **living**：as-built 运行时全貌（wakeup 生命周期、真实事务模型、调度相位） |
| [`AGENT_CONTRACT.md`](./AGENT_CONTRACT.md) | Agent 接口契约；**部分历史**（§4 subprocess/gateway 为 v0.x，见横幅） |
| [`AGENT_RUNTIME.md`](./AGENT_RUNTIME.md) | Agent 运行时设计；**部分历史**（§2-§4 v0.x；§3.6-3.8 / §5.5 current，见横幅） |
| [`TRANSACTION_BOUNDARIES.md`](./TRANSACTION_BOUNDARIES.md) | 一次唤醒的事务边界；**部分历史**（Step-9 单提交点模型已被"工具时即时持久"取代，见横幅） |
| [`PERSISTENCE_SCHEMA.md`](./PERSISTENCE_SCHEMA.md) | 持久层 schema（部分滞后于实现，以 `migrations/0001_initial.sql` 为准） |
| [`PERSONAS.md`](./PERSONAS.md) | Persona 设计（v0.x roster；现役 persona 清单以 `src/lyre/personas/` 为准） |
| [`DASHBOARD.md`](./DASHBOARD.md) | Owner 观测面板（FastAPI + HTMX + SSE） |
| [`WORKFLOW_ORCHESTRATION.md`](./WORKFLOW_ORCHESTRATION.md) | 确定性编排：mailbox-native fan-in barrier + OTP 式 supervisor/reaper，**已落地** |
| [`AGENT_THREADS.md`](./AGENT_THREADS.md) | `thread_id` 主线上下文连续性 + 有界自驱动，**已落地** |
| [`LONG_RUNNING_ROBUSTNESS.md`](./LONG_RUNNING_ROBUSTNESS.md) | 长跑健壮性（一）：压缩硬化 + 记忆卫生，**已落地** |
| [`LONG_RUNNING_ROBUSTNESS_2.md`](./LONG_RUNNING_ROBUSTNESS_2.md) | 长跑健壮性（二）：恢复诚实性（lease fencing / max_turns 分类）+ 反失控 + 索引卫生，**已落地** |
| [`LONG_RUNNING_ROBUSTNESS_3.md`](./LONG_RUNNING_ROBUSTNESS_3.md) | 长跑健壮性（三）：无界增长与空转收口（D1/C4；H2 设计定稿待实现），**部分落地** |
| [`FAILURE_ROBUSTNESS.md`](./FAILURE_ROBUSTNESS.md) | 失败韧性：dispatcher 监督缺口 + LLM 调用重试/failover，**已落地** |
| [`ORCHESTRATION_ROBUSTNESS.md`](./ORCHESTRATION_ROBUSTNESS.md) | 编排健壮性：fan-in 失败可见 + typed-result 强制 + 轮次预算，**已落地** |
| [`MEMORY_ORGANIZATION.md`](./MEMORY_ORGANIZATION.md) | memory facts 的整理与淘汰（index 分组 + archive），**已落地** |
| [`CAPABILITY_DISCOVERY.md`](./CAPABILITY_DISCOVERY.md) | 外部 coding agent 委派 + 能力固化为 skills；含现实安全边界声明 |
| [`BUILTIN_SKILLS.md`](./BUILTIN_SKILLS.md) | 出厂技能库机制（直接读 package），**已落地** |
| [`OWNER_AS_CHAT_PARTNER.md`](./OWNER_AS_CHAT_PARTNER.md) | Owner 异步聊天界面（Lark 通道）设计 |
| [`PLUGINS.md`](./PLUGINS.md) | 插件系统 spec，**parked**（owner 决议 2026-06-10；gateway 类接缝若复活走此弧线） |

---

