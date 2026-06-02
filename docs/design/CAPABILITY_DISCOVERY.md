# Lyre — 能力发现与固化（外部 coding agent 委派）

> **文档定位**：定义 Lyre 如何把"写代码"这类重活委派给**外部 coding agent**（Codex /
> Claude Code / aider / cursor-agent / gemini-cli / opencode / …），并且**不在机制层为每个
> variant 写适配器**。核心结论：适配是**被 agent 发现、再固化成 skill** 的知识，机制层只提供
> 一个 variant-agnostic 的薄地基。本文是定稿，配套分 PR 路线图（§7）。
>
> **相关**：[`FOUNDATION.md`](./FOUNDATION.md) 五条铁律；[`WORKFLOW_ORCHESTRATION.md`](./WORKFLOW_ORCHESTRATION.md)
> 监督/回收层；`runtime/skills.py` skill 加载；`runtime/tools/shell.py` + `runtime/shell.py` 沙箱。
>
> **English one-liner**: Delegate coding to best-in-class external coding agents, but adapt to
> each CLI variant at the *agent layer* — an agent discovers how to drive a CLI and persists the
> recipe as a governed skill — so the runtime stays thin (one credential seam, no per-variant code).

---

## 1. 背景与中心张力

Lyre 现在的 worker（`worker-maintainer`）用 `shell_exec` / `python_exec` **自己撸代码**。但在"自主写
多文件代码"上，它干不过专门的 coding agent —— 后者有自带的 agentic loop、文件编辑、跑测试、自我
纠错。所以 worker 应该升级为 **coding agent 的编排者 + 验收者**：拆任务 → 写清楚 spec → 调用 coding
agent → **review diff / 跑测试 / 卡 PR** → 用 mailbox 汇报。重活外包给最强的，Lyre 做它擅长的编排、
持久化、多 agent 协调、把关。

**中心张力**：coding agent 的 variant 是**长尾**，每个的 CLI flags / headless 调用 / 输出格式 / auth /
sandbox flag 都不同。**在机制层（core 代码）为每个 variant 写适配器追不过来**——这条路必然腐化。

**结论**：把适配推到 **agent 层**。一个 agent **发现**某个 coding CLI 怎么用（跑 `--help`、读文档、
smoke-test），把"配方"**固化**成可复用的、scoped 的 skill。新 variant = 新发现的 skill，**core 零改动**。

---

## 2. 核心原则

- **机制层薄、variant-agnostic、只适配一次**。它不认识 codex 还是 claude；它只提供：(a) 让 agent 跑
  外部命令的能力（已有 `shell_exec`），(b) 受控地给那个命令喂**一份具名凭证**（唯一硬增量，见 §5），
  (c) 一个把发现的知识固化成 skill 的治理通道（§4，**已有**）。
- **适配是数据，不是代码**。"怎么驱动 coding-CLI-X" 是一个被发现、被审批、被持久化的 **skill**。
- **不自改 persona**（§6）。学到的东西进 notes/scratchpad；角色演进走 PR 或 propose→approve。

---

## 3. 已有地基（不用新建）

落地前盘点，Lyre 其实**已经具备**大半：

| 地基 | 现状 | 出处 |
|---|---|---|
| skill 三层目录 + 只 surface `approved/` | `~/.lyre/skills/{approved,proposed,archived}/`，loader **只把 `approved/` 注入 prompt**，`proposed/` 标注 under-review、不加载 | `runtime/skills.py`（`_ensure_skill_dirs` / 扫描注释） |
| skill 治理流 | `SkillRepository.propose` / `approve` 已存在（repo 层） | `persistence/repositories.py` |
| scoped 加载 | skill frontmatter 可按 persona scope 过滤、按需加载 | `runtime/skills.py` |
| FS 可写 | `shell_exec` 的 cwd jail **不是 FS jail**；`python_exec` 接受任意 `cwd`、能 `open()` 绝对路径 → agent **已经能写** `~/.lyre/skills/proposed/` | `runtime/shell.py` / `runtime/tools/python.py` |
| secret 隔离 | `shell_exec` env 白名单**故意 strip 掉** `ANTHROPIC_*`/`LYRE_*` → 这是当前**唯一**真正的 containment（因为 FS 没 jail） | `runtime/shell.py:6,35` |

**两个推论**：
1. "固化"**不需要新工具**——写文件进 `proposed/`（python_exec 现成）+ loader 已只认 `approved/`。
2. 唯一硬增量是**凭证**：secret 被 strip，coding CLI 跑不起来（没 auth）。见 §5。

---

## 4. 固化流：discover → propose → review → promote（无新工具）

```
[发现] worker / research agent（有 shell+python；dispatcher 没有）：
   跑 `codex --help` / 读文档 / 在一次性 worktree 里 smoke-test 一个 trivial 任务
   → 确认 headless 调用方式、prompt 怎么传、diff 在哪、要哪个 credential bundle、sandbox flag
   → 把 recipe 写成 skill 草稿到 ~/.lyre/skills/proposed/<name>/SKILL.md（python_exec 写文件）
   → mailbox_send 把 recipe 正文发给 dispatcher（dispatcher 读不到 skills/，靠邮件评审）

[评审] dispatcher 收到后 dispatch 一个 reviewer 或 researcher 去评审（**不是 dispatcher 自己评**）：
   检查：recipe 安全（无 `curl|sh`、只用声明过的 credential bundle、scope 收敛）、
        有 smoke-test 证据、调用方式可信
   → reviewer 把评审结论 mailbox 回 dispatcher

[决策+晋升] dispatcher 综合评审结论**最终决定**：
   通过 → dispatch 一个 worker 去做 FS move：mv skills/proposed/<name> skills/approved/<name>
          （dispatcher 自己没有 shell/python，不能动 FS；它编排，worker 执行）
   不通过 → 让 worker 移到 archived/ 或删除，反馈发现者

[复用] 后续任意 worker 的 coding 任务：
   按需加载这个 approved 的 scoped skill → 用 shell_exec + credential bundle 驱动 CLI
   → 捕 `git diff` → review / 跑测试 → mailbox 报告
```

**谁干哪步（铁律：dispatcher 编排、不执行 FS）**：
- **发现 + 写 `proposed/`**：worker / research agent（需 shell+python）。
- **评审**：reviewer 或 researcher（读邮件里的 recipe 正文）。
- **决策 + 触发晋升**：dispatcher（综合评审结论；派 worker 做 FS move）。
- **FS move（promote）**：worker（dispatcher 派的）。

---

## 5. 唯一硬增量：凭证 broker（gated `shell_exec` opt-in）

coding CLI 需要它自己的 API key，但 `shell_exec` 把 secret 全 strip 了。所以加一个**受控的、具名的**
凭证注入：

- **config 声明 bundle**（`config.toml`）：
  ```toml
  [coding_backends.codex]
  auth_env = "OPENAI_API_KEY"     # 持有 secret 的 env 变量名（值留在 env，不进 config）
  # allowed_personas = ["worker-maintainer"]   # 可选：限定哪些 persona 能用
  ```
- **`shell_exec` 加一个 opt-in 参数** `credentials="<bundle>"`：默认无（= 今日行为）；传了就把
  `os.environ[bundle.auth_env]` 注入**那一次** subprocess 的 env（且仅注它）。bundle 必须已配置 +
  （可选）persona 在 allowed_personas 内。
- **安全分工**：**agent 发现"怎么用"（flags/调用方式）；owner 在 config 里 provision "密钥"（env 名）；
  propose→approve 治理"固化"**。agent 不发现、不接线 secret。

### 已知风险（明确接受）
`shell_exec` 是任意 argv 面，且 FS/网络**没有真 jail**——所以注入的 key 能被 worker 自拼的任何命令读到
（`printenv`、读子进程 `/proc/<pid>/environ` 等）。**在 Lyre 的单 owner 威胁模型下接受**：你的 key、你的
机器、worker 就是你的 agent，主要风险是"一个犯傻/幻觉的 LLM 误用 key"，有界。**真正的隔离**（网络
egress 限制、独立 uid、短时/降权 token）**绑定到后续的沙箱硬化 TODO**（`shell.py` docstring 已预告 per-task
ephemeral key 方向）。MVP 不做窄面 recipe-runner。

---

## 6. 明确非目标

- **agent 自改 persona / append**。学到的东西 → notes/scratchpad；角色演进 → 刻意的 persona/APPEND 编辑
  （一个 PR）或受治理的 propose→approve。persona 必须**字节稳定**（prompt-cache）+ `allowed_lyre_tools` 是
  提权面（治理）。
- **core 里的 backend registry**（per-variant 适配进 core）—— 正是本设计要避免的。
- **多租户硬隔离 / 不可绕过的凭证保护** —— 留给沙箱硬化；本设计在单 owner 模型下用治理 + env 隔离 +
  worktree jail 作为现实边界。
- **窄面 recipe-runner（key 不进 worker 手）** —— v2 备选，MVP 不做（见 §5 已知风险）。
- **递归无界**：Lyre worker → `claude -p` 不应再 spawn lyre/另一个 coding agent；recipe/工具里封顶。

---

## 7. 安全与治理（这是换灵活性的代价，且是对的代价）

一个固化的 recipe 本质是"**被批准的、带凭证的任意代码执行**"。所以：
- **propose→approve 不可省**：reviewer/researcher 评审 + dispatcher 决策；首次信任任何 backend，owner 在环。
- **凭证只给 owner 在 config 声明过的 bundle**，agent 不能凭空拿 key。
- **发现必须 smoke-test 后才固化**；recipe 可带 `validated_at`；CLI 改 flags → recipe 腐化 → 重新发现。
- **worktree jail** 始终在；coding agent 跑在 per-task worktree 内。

---

## 8. 分 PR 路线图

| PR | 内容 | 验收 |
|---|---|---|
| **CD-1** ✅ 本文档 | 设计定稿（本文件） | review anchor |
| **CD-2** 凭证 broker | `[coding_backends]` config（bundle name → auth_env [+ allowed_personas]）；`shell_exec` 加 `credentials` opt-in，受控注入；scheduler 把 config 传进 tool extras | 配置解析 / 注入生效 / 未配置 bundle→error / persona 不允许→error / 不传→今日行为（无 secret） |
| **CD-3** 发现/固化/晋升 persona 流 | dispatcher（coding 任务优先找 approved skill；无则派发现任务；proposed→路由 reviewer→派 worker 晋升）+ reviewer（评审 proposed capability skill 的安全判据）+ worker-maintainer（发现/晋升/用 approved skill 跑 CLI）的 persona 文档 | persona 解析/allowlist 正确；流程文档自洽 |

> **依赖**：CD-3 的 smoke-test 依赖 CD-2 的凭证（跑 CLI 要 auth）。CD-2 依赖 CD-1 的设计。

---

## 9. 与五铁律的关系

- **铁律一（provider 中立）**：coding backend 是**外部 agentic CLI**，经 `shell_exec` 调用——不是 LLMAdapter
  那个"单轮 chat completion" seam，不进 `adapter/`。运行时仍 provider-中立。
- **铁律二（Lyre 是网关）**：凭证注入经 `shell_exec`（受控、worktree jail、config-gated）——正是网关在中介
  一个有权限的对外动作。
- **铁律三（拔线）**：发现/晋升都是文件 + mailbox + task（已提交行）；coding agent 跑在 worktree，被
  SIGKILL → worktree 重 provision → 重跑干净。
- **铁律五（mailbox 唯一）**：发现→评审→决策全走 mailbox（recipe 正文、评审结论），无 sidechannel。
