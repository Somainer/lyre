# Owner-as-Chat-Partner

> **English TL;DR**: Owner is offline, asynchronous, low-bandwidth.
> Their interface is chat (Lark / Slack / future IM), not the
> dashboard. The dispatcher is the owner's single conversation partner
> — event-driven, never blocking, throttled to send mail only when
> there's substance; reactions echo to the chat surface for silent
> acks. The dashboard is observation, not workflow.

## 心智模型

Lyre 的 owner **不是**坐在 dashboard 前看 task tree 的运维。他是：

- **离线**：发完一条「帮我做 X」就走开，可能几小时 / 一晚 / 一天后才回来看
- **异步**：他的每次「输入 → 输出」之间允许有任意长的间隔
- **chat-first**：他的 UX 是 Lark / Slack 的私聊 thread，不是浏览器
- **低带宽**：每条主动推给他的消息都是一次打扰（手机推送、桌面通知）

dashboard 是 **observer surface**——他偶尔来看看「我离线这段时间发生了什么」，
但 workflow **完全发生在 mailbox**。

## 三条系统性约束

### 1. dispatcher 必须 event-driven，禁止 blocking

dispatcher 是 owner 的唯一对话伙伴（singleton kind）。**它的 wakeup 不能挂死在
任何「等子任务」的状态**——否则 owner 给他发新事时，scheduler 看到 dispatcher 有
active task 就跳过 Phase 0 auto-wake-on-mail，owner 等于在没事干的时候被拒接。

具体落实：
- **dispatcher.allowed_lyre_tools 不含 `await_subagents`**——物理上调不到
- 模型在 prompt 里被反复教育：dispatch 完后停止调 tool，让 wakeup 关闭
- worker / analyst 跑完会 `mailbox_send` 回 dispatcher，auto-wake-on-mail 起新 wakeup
- 同期 owner 新消息走同一通路，互不阻塞

`await_subagents` 工具本身保留，给 **analyst / reviewer** 这种「我必须凑齐所有 child
输出再写 final answer」的合成型角色继续用——它们的 task 就是 composed answer，
等所有 child 完成是合理的；它们也不是 owner-facing singleton。

### 2. owner-bound 邮件 = chat 推送通道，必须节流

之前的规则「每条 owner 消息都必须有回应」是 dashboard 心智下的产物。chat 心智下：

- **FYI / 闲聊 / 不需要 owner 做事** → `mailbox_react(kind="ack")` → 外部 channel
  上原消息加 ✓ emoji（owner 看到「已收到」但**没**推送通知）
- **有结论 / 有问题 / 有承诺 / 需要 owner 做事** → `mailbox_send` 正经回信
- **完全不需要让对方知道** → `mark_read`

**聚合**也由模型自己判断。prompt 给的启发：
- 同期多 worker 报告 → 用 `mailbox_send(to=self, deliver_in="10m")` 推迟自己，
  让多份报告凑齐再综合发 owner
- 不规定窗口 / 阈值，让模型在跟 owner 的长期摩擦中自己学

**怎么学**：dispatcher 的 `agent-<id>-notes.md` 里 runtime 自动追加 wakeup 摘要。
模型读自己最近几次给 owner 的 reply 习惯——owner 后续抱怨「太吵」/「太啰嗦」
时，把教训手写进 notes，下次 wakeup 在 prompt 里读到。**runtime 不写死回信模板**。

### 3. owner 一次请求 = 一个 chat thread

owner 在 Lark 发条「重构鉴权」。整个完成过程他在 thread 里**只**看到：
- 他自己的初始请求
- dispatcher 一两条进度（仅当跨天 / 主动决定要中间更新）
- dispatcher 的最终结果**或**需要他决定的请示

dispatcher↔analyst↔worker↔reviewer 内部往来 **完全不暴露**给 owner channel。

技术保证：
- 只有 `recipient="owner"` 的 mail 走 `owner_mail_enqueuer` → `channel_publish`
- dispatcher 发 owner 时永远 `reply_to=<owner_msg_id>` → Lark 端续 thread
- worker 直接给 owner 发 blocker 邮件的特殊情况：考虑改成走 dispatcher 转发，
  避免 thread 串不起来（**待办**）

## Reaction 跨通道映射

| Lyre reaction kind | 含义 | Lark 映射 | 推送通知 |
|---|---|---|---|
| `ack` | "我看到了，无须行动" | `OK` emoji | ❌ |

未来：`thumbs_up` / `eyes` / `confused` 等。每个 channel 在自己的实现里维护一张
`kind → 原生 primitive` 的映射表（见 `LarkChannel._REACTION_TO_LARK_EMOJI`）。

机制：`mailbox_react(msg_id, kind)` 写 `mail_reactions` 表后，若原 mail 的
`metadata.channels.<name>.message_id` 存在，则对每个这样的 channel enqueue 一个
`channel_reaction_publish` outbox 行。dispatcher 走 `ExternalChannel.publish_reaction`
调用对应 channel 的 API（Lark: `im.v1.message_reaction.acreate`）。

kill-test 安全：outbox `UNIQUE(kind, external_id="channel:<name>:reaction:<msg_id>:<reactor>:<kind>")`
保证不重复，崩了重启自动续跑。

## 任务跨度 vs owner 期望

| 任务跨度 | dispatcher 行为 |
|---|---|
| 秒级（trivial） | 直接派 worker，跑完 react 或 send 一次完结 |
| 分钟级 | 同上 |
| 小时级 | 跑完一次 send；过程不打扰 owner |
| 天级 | 视情况中间一两次 progress send（"还在跑，预计 X 完成"），其他用 react/scheduled reminder 自己跟 |
| 卡住 | urgency=blocker mail 请示 owner，写清楚选项 |

判断「中间要不要更新」由模型在跟 owner 的长期摩擦中学。

## 跟 dashboard 的关系

dashboard 不消失，但定位变了：

- **owner 离线时**：dashboard 只是日志看板。owner 不来。
- **owner 偶尔登录**：看 task tree / agent occupancy / 最近 N 小时活动，理解整体节奏
- **debugging**：开发者 / owner 撞到 dispatcher 决策古怪时，从 dashboard 倒查
- **chat 没有的能力**：cancel / re-run / fork task，这些在 dashboard 做

dashboard 上的 reaction 一直能看（直接读 `mail_reactions` 表）；新增的是
chat 端也能看到（通过 `channel_reaction_publish` outbox + Lark API）。

## 已知尚未实现 / 待办

- **worker → owner 的 blocker 直发**：当前 worker 可以 `mailbox_send(to="owner", urgency="blocker")`，
  会被 owner_mail_enqueuer 推到 Lark。但 thread 关系串不起来（不是 owner 之前 thread 的回复）。
  方案候选：强制走 dispatcher 转发，dispatcher 加 forward 字段
- **digest / 「我离线期间发生了什么」**：owner 隔几小时回来想要一份摘要，目前没有
  机制主动给。可以靠 scheduled_mail 自己定时给自己发 reminder，但风格上不优雅
- **owner-side reaction 入站**：owner 在飞书给 dispatcher 的消息加 emoji，目前不
  解析。`mailbox_react` 现在是单向（runtime → channel），双向解锁是另一个 PR
