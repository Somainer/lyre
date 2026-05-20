# Lyre

> 长期运转的个人多 agent 运行时。Agent 持久存活、只通过 mailbox 通讯、跨多次唤醒推进一个目标。

Lyre 在你的机器上跑一支小规模的 AI agent 团队。每个 agent 在 SQLite 里有自己的 mailbox。Agent 之间**只**通过 mail 通讯——不能互相直接调用。工作发生在 **wakeup** 里：一次 wakeup = 一个 agent 跑一轮 tool-using LLM loop，读邮件、动手、回信、然后 idle。一个 task 是 agent 跨多次 wakeup 持续推进的目标，状态完全落在持久层里。

Provider 中立：Anthropic、DeepSeek（Anthropic-compat 或 OpenAI-compat）、OpenAI、OpenRouter、vLLM——都在同一个 `LLMAdapter` 接口后面。换 provider 只是改一个 YAML。

## Lyre 跟其他东西哪里不一样

- **长期运行，不是 session-based**。一个 task 不是一条 prompt，而是 runtime 跨多次模型 wakeup 推进的目标；进程重启、机器重启都不丢，靠 SQLite 落盘。

- **只走 mailbox 通讯**。Owner → agent、agent → agent、未来的定时任务都流经同一份持久 mailbox。Dashboard、CLI、agent 本身只是这一份 mailbox 的三种 client。

- **个人 infra，不是组织 infra**。单 owner，没有多租户隔离、没有 RBAC、没有合规面。围绕*你自己*的工作流优化，不为团队。

- **拔线测试是判据**。任意时刻 kill 任意进程，系统都能恢复。这一条直接推出了 outbox 模式、租约式 task ownership、per-message 读状态、可追溯的 transcript。

## Quick start

```bash
git clone https://github.com/Somainer/lyre.git
cd lyre
uv sync

# 至少配一个 provider 的 key（DeepSeek 最便宜）
export DEEPSEEK_API_KEY=sk-...    # 或 ANTHROPIC_API_KEY、OPENAI_API_KEY

uv run lyre onboard               # 交互式 wizard：身份 / provider / DB / 目录
uv run lyre serve                 # scheduler + dispatcher + dashboard
```

另起一个终端：

```bash
uv run lyre send leader "Hi leader, reply with pong and tell me what model you're on."
uv run lyre mailbox owner --unread-only      # 看回信
open http://127.0.0.1:8765                   # 或者开 dashboard 看
```

完整的 5 分钟 walkthrough 见 [docs/getting-started.md](./docs/getting-started.md)。

## 文档

| 文档 | 什么时候看 |
|---|---|
| [docs/getting-started.md](./docs/getting-started.md) | 首次运行 |
| [docs/concepts.md](./docs/concepts.md) | 想理解 agent vs persona、wakeup、mailbox、五条铁律的心智模型 |
| [docs/configuration.md](./docs/configuration.md) | 配 env、换 model、改路径 |
| [docs/writing-personas.md](./docs/writing-personas.md) | 加新 agent 类型或写 skill |
| [docs/cli-reference.md](./docs/cli-reference.md) | CLI 命令参考 + debug recipe |

架构设计文档（中文）在 [docs/design/](./docs/design/)：`FOUNDATION.md`、`AGENT_CONTRACT.md`、`TRANSACTION_BOUNDARIES.md`、`PERSISTENCE_SCHEMA.md`、`AGENT_RUNTIME.md`、`PERSONAS.md`、`DASHBOARD.md` 记录了设计决策*为什么*这样做，是 contributor 改内部实现的参照。

英文版 README 在 [README.md](./README.md)。

## 状态

Lyre 由单 owner 主动开发中。五条核心架构铁律（见 [docs/concepts.md](./docs/concepts.md#the-five-laws)）已稳定，不会再动。这层之上的接口——persona 格式、工具集、dashboard UI——还在演化。第一个 tag release 之前请预期 commit 间的 breaking change。

欢迎使用、fork、学习、贡献。开发循环见 [CONTRIBUTING.md](./CONTRIBUTING.md)。

## License

[MIT](./LICENSE)。
