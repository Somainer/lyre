# Lyre 深度 Review（实现 + 理念）与熵减 / 迭代计划

> **文档定位**：2026-06-10 对整个代码库与文档正典的一轮多智能体深度 review 的综合结论。
> 方法：13 个子系统/视角深读 agent（9 子系统 + 理念 + 路线图 + 测试审计）→ 2 个跨模块审计（五铁律、熵）→ 对每个 critical/major finding 的对抗性验证（critical 三票面板，major 单票）→ 完备性批判。共 80 个 agent、~670 万 subagent tokens。
> 结论置信度标注：**confirmed** = 对抗验证通过；**partial** = 问题真实但原表述有偏（已按更正陈述）；minor 级未做对抗验证。
>
> **English one-liner**: Full-codebase multi-agent review: 145 findings (2 critical, 63 major — 45 confirmed, 15 partially-correct, 0 refuted), a five-laws audit, a doc-canon coherence verdict, and prioritized entropy-reduction + feature-iteration plans.

---

## 0. 总评

**实现质量显著高于同类个人项目的基线。** 884 个全离线测试（~25s，76% 覆盖）、ruff + strict mypy 全绿；幂等纪律（确定性 external_id 贯穿所有系统邮件）、租约 fencing、fan-in 的 mail-before-flip kill-safe 设计、原子写规约（tmp+rename + fsync 注释引用铁律）都是真实落地的工程，不是文档愿景。对抗验证 0 个 finding 被驳回，说明问题列表里没有水分——但也说明下面每一条都值得认真对待。

**最大的结构性问题不在代码，在正典。** 四份奠基文档（FOUNDATION / AGENT_CONTRACT / TRANSACTION_BOUNDARIES / AGENT_RUNTIME §2-4）描述的是一个**从未建成**的架构（fork subprocess + per-task tmpdir + ephemeral SSH key + Unix-socket MCP gateway + Step-9 单一原子提交点 + Postgres），而且自称 status: settled / 定论；CLAUDE.md 本身还在教一个**已被删除**的调度模型（parent-resume / needs_input-awaiting-subagents / 三相 tick）。新文档（Jun 4-10 的 PR-round docs）质量极高但呈 append-only 堆积——没有任何一份文档描述今天的运行时全貌。"铁律二"在正典内部有两个互相矛盾的定义，实现符合其中一个。这是本轮 review 最该优先偿还的债。

**两个 critical 都是拔线测试（铁律三）的直接违反**，且都可低成本修复。

---

## 1. 实现 Review

### 1.1 值得保住的强项（各子系统 reviewer 一致确认）

- **幂等与 kill-safe 纪律**：每封系统邮件都有确定性 external_id（`sched:<id>:<occ>`、`fanin:<g>:<leg>:failed`…）；fan-in 解析 mail-before-flip + 单赢家 guarded UPDATE；notes rotation 是 archive-append-fsync 先行 + 原子重写 + by-wakeup-id 去重。
- **A1 租约体系端到端自洽**：claim/renew/release/update_checkpoint/update_status 全部 fence 在 lease_holder 上；heartbeat 自我 fencing。
- **compact.py 的 pivot 规则**（锚定倒数第 K 个 assistant message）从构造上保证 tool_use/result 对与 thinking-block-first 顺序不被切断；compaction artifact 携带保证重压缩幂等，且有针对性测试。
- **铁律一的 import seam 双向干净**（grep 验证：provider SDK import 仅存在于 adapter/ 内；compact/wakeup_summary 的旁路 LLM 调用也走 LLMAdapter）。shell 的 env allowlist 同时是中立性屏障（子进程看不到 ANTHROPIC_*/LYRE_*）。
- **测试套件的"文档性回归测试"文化**：docstring 引用生产事故、设计文档条款（A1/A2/RB-2/Q5/铁律五）与确切失败机理；chaos 测试有真 SIGKILL + 双写者 WAL 测试。
- 时间处理（全 tz-aware UTC）、日志（仅 structlog）、ID 生成（行用 uuid7 / external_id 用 uuid4）全库一致——熵审计确认这三类常见烂账在本库不存在。

### 1.2 Critical（均 confirmed，均为铁律三违反）

| # | 位置 | 问题 |
|---|---|---|
| C-1 | `scheduler.py:1552` | **wakeups.start 与 claim_lease 之间被 SIGKILL → 该 agent 永久砖死**。wakeup 行先 INSERT（commit 1）再 claim lease（commit 2）；中间被 kill（或 claim 抛 busy_timeout）留下 open wakeup row + status='pending' 无租约的任务。`has_active_for_agent` JOIN 把 pending 也算 in-flight，Phase 3 从此永远跳过该 agent 的所有任务；唯一清扫者 `close_orphans_for_task` 又躲在被锁死的那个检查后面；`find_expired_leases` 只看 in_progress。修复：先 claim（带预铸 wakeup_id）再插行，或扩展启动审计修复 pending+无租约上的 open wakeup。 |
| C-2 | `introspect.py:201` | **update_scratchpad 非原子写**。scratchpad 是短期工作记忆档位（identity preamble 教每个 agent 醒来先读它），但 `path.write_text()` 是 truncate-then-write——写中 SIGKILL 留下空/半截文件，工作记忆无声毁灭且无任何恢复源。库内 wakeup_summary 已有正确的 tmp+replace 模式。修复：抽公共 `atomic_write_text` helper + chaos 测试。 |

### 1.3 Major 主题聚类（45 confirmed + 15 partial；编号引自 findings 全表）

**A. 适配器层——默认 provider 的诚实性被系统性破坏**

- **[20] Anthropic adapter 永远丢弃真实 stop_reason**（confirmed）：message_delta 分支里 `if usage is not None: return Usage(...)` 永远先赢（SDK 的 usage 是必填字段），TurnComplete 分支不可达；MessageStop 无条件发 `end_turn`。后果：A2 辛苦建立的"诚实截断分类"在 Anthropic（默认 provider）上完全失效——max_tokens 截断被分类为 completed。这是单点小修，优先级应最高。
- **[21] 熔断器首个 cooldown 后永不再开**（confirmed）：`mark_failure` 只在 `opened_at is None` 时设值；half_open 中失败不再翻开电路。死 provider 只被保护 180 秒，此后每个 wakeup 每 turn 都付全额超时。
- **[24] registry 给 Anthropic 条目标了 `reasoning` 能力但 adapter 从不发 thinking 参数**（partial-confirmed）：requires:[reasoning] 的 persona 路由到 Opus 后静默拿不到 extended thinking；跨 provider fallback 还会回放对方签名的 thinking block 被 Anthropic 拒收（[6]，reachability 较原述窄但真实）。
- **[23] Responses adapter 无法回放 reasoning 模型的工具轮**（confirmed）：reasoning items 被丢弃，无 encrypted_content/previous_response_id 路径——对 o-series/gpt-5 的多轮工具调用根本不工作。
- **[22] openai.py 无条件发 `max_tokens`**（partial）：gpt-5/o-series 在 chat completions 上拒收该参数（要求 max_completion_tokens）；shipped registry 的 gpt-5 条目默认未启用所以今天无伤，但"打开即坏"。
- **[42] AnthropicAdapter.stream_turn 零流式测试**（55% 覆盖）而 OpenAI 有完整 mock-stream 测试（95%）——默认 provider 反而是测试最薄的。

**B. 调度器 / 编排——崩溃恢复语义的二阶缺口**

- **[7] per-agent 串行化在 subprocess 模式有两个洞**（confirmed）：子进程 Python 启动 > 1s tick，下一 tick 看不到 wakeup 行 → 同一 agent 双 wakeup 并发踩 scratchpad/notes（5.5.1 节明令禁止的事）；Phase 2 恢复路径完全没有 has_active 检查。
- **[8]+[9] fan-in 晚到结果 × O2 降级 × transient 重启 = 无界烧 LLM 循环**（confirmed）：quorum 解析后 straggler 的结果被拒 → 干完活的 leg 被记 failed → reaper 重启 → 又晚到 → 循环；60s 滑窗强度限制对以 wakeup 为节拍（分钟级）的失败永远不触发。另外重启后 roster 不指向新任务、O1 哨兵在 Phase 0.8 重启决策之前插入造成过早 quorum。
- **[10] ephemeral 重启并非 one-for-one**（confirmed）：丢 git_context（重试在空 worktree 里跑）、丢 tier_overrides/max_turns（按导致失败的默认值重跑）、丢 lease_duration/deadline。
- **[11] 三条任务失败路径不发 task_terminated 邮件**（partial-confirmed）：git provisioning 失败还留下永久 open 的 wakeup 行；父级/owner 对非 fan-in 子任务之死一无所知。
- **[12] Phase -1/0/0.5 无逐行错误隔离**（partial）：一个毒行（坏 recur_kind 等）每 tick 在任务派发之前炸掉整个 tick——全系统饿死，只有日志可见（而日志未配置，见 §3.4）。C4 维护阶段已经按同样理由包了 try/except，这三个阶段没包。
- **[0] 即 C-1**（见上）。

**C. 持久层**

- **[13]/[16]/[54] 共享连接 transaction() 回滚可吞掉并发写者已 ack 的写入**（partial-confirmed，三个 reviewer 独立发现）：commit 锁只守 commit 边界，语句自由加入连接级开放事务；A 回滚时 B 已执行的 INSERT 一起消失，B 随后 commit 空事务并报成功（最坏：outbox 标 dispatched、mail 行不存在 = 静默丢信）。可达性窄（要求 serve 模式下并发 transaction() 用户）但机制真实可复现。修法三选一：写者持锁覆盖 statement+commit / dispatcher 独立连接 / transaction() 回滚前校验无外部语句插入。
- **[14] `_row_to_task` 读回时静默丢 5 个时间戳列**（confirmed）：dashboard 时间线全部 lex-sort 到开头（与已修过的 delivered_at 回归同模式）、query_task_status 给 agent 报 null。
- **[18] migrations 目录没打进 wheel，靠无界向上文件系统行走定位**（confirmed）：非 editable 安装必死或更糟——撞上任意同名目录就 executescript 外来 SQL。应移入 `src/lyre/data/migrations/`。
- **[15] run_maintenance 绕过 commit-lock 协议**（partial：今天恰好不可达，但它是把 private-but-load-bearing 锁协议留给未来踩的雷）。
- **[19]/[52] 死表死仓**：skills/artifacts/local_hot 三套 table+repo+Protocol 零运行时调用者（artifacts.insert 在 dedup 冲突时还返回一个不对应任何行的编造 id）。

**D. 记忆 / 人格契约——"agent 被教的"与"运行时做的"漂移**

- **[4]/[27]/[64] spawned agent 的自动摘要写进无人读的路径**（confirmed）：`wakeup_summary` 用原始 `persona/name` id 拼 notes 路径而其它四处全部 flatten——所有 spawn_only worker 的长期记忆契约静默断裂。库里有标着 "Centralised so the runtime and the tool never disagree" 的 helper，四处 inline 它、第五处忘了它——熵直接产出了 bug。
- **[28]/[29] persona 正文与 shipped checklist 教扁平 skill 路径，而 loader 只认 `<name>/SKILL.md` 目录形**（confirmed）：照章办事的 reviewer 每次 dedup 检查都在 ls 一个不存在的目录。
- **[3] compaction 把失败的 mailbox_send 记成 '[Sent ...]'**（confirmed）：压缩后模型相信已回信，停止重试，asker 永远收不到——恰好发生在其声明的保真层。**[5]**（partial）thrash-bail 还会向已经回过信的 asker 发"未回信"的虚假道歉邮件，且截断任务以 completed 终止。
- **[30] skill 加载诊断算了但从不输出**（confirmed）：审一个 frontmatter 坏掉的 skill 过会后它静默不加载，没人知道。

**E. 工具 / 安全面**

- **[25] mailbox_read 读他人收件箱会把对方的邮件标已读**（confirmed）——压掉对方的 auto-wake；工具描述还主动邀请这种用法。
- **[26] cancel_scheduled_mail 无所有权检查**（confirmed）：任何持有该工具的 agent 可解除他人的监督超时/循环预算/owner 定时邮件。
- **[38] dashboard 状态变更 POST 无 CSRF**（confirmed）：表单导航豁免于 CORS，任意网页可以 owner 身份向 agent 注入邮件（agent 持有 shell_exec）。DASHBOARD.md 承诺过的 token 方案没实现。
- **[39] Lark `_handle_inbound`——唯一互联网入口的授权边界——零测试覆盖**（confirmed）。
- **[37] OwnerMailEnqueuer 先 catch_up 后 subscribe**（confirmed）：窗口期 owner 邮件永不到外部通道直到下次重启（而启动后恰是 agent 集中汇报的时刻）。修复：先订阅后扫描（outbox 幂等让重叠免费）。
- **完备性批判补充（已抽查证实）**：credential bundle 以明文 env 注入 agent 自选命令——`shell_exec(command="env", credentials=<bundle>)` 把密钥原文返回给模型并落入永不清理的明文 transcript。"never returned to the model" 的声明不成立。单 owner 信任模型下可接受，但需作为显式决定记录，而非靠误述。

**F. 配置 / CLI / Onboard**

- **[31] re-onboard 静默重置 owner 自定义的 [[models]] 字段**（confirmed）：tier/capabilities/enabled/context_window 全部被硬编码值覆盖——保留修复（#50）对 [[models]] 不完整。
- **[57]/[60] 文档承诺的 LYRE_DEFAULT_MODEL fallback 不存在**（confirmed）：无 model_preference 的 persona 直接 RuntimeError 炸 wakeup。要么实现要么删文档+删 Config 字段。
- **[59]/[34] 解析了但从不消费的旋钮**：auto_wake_on_mail（serve 不传给 Scheduler）、default_dashboard_port（click 硬编码 8765 永远赢）。
- **[33]/[32] 文档与代码语义相反**：[[models]] 是整体替换不是合并（三处文档说合并）；.env 优先级 home 赢而 docstring 说 CWD 是 dev override。
- **[58] agent 创建规则四处重复实现且语义分歧**（partial-confirmed）：CLI 路径绕过 singleton 检查（可造出第二个 dispatcher）和 id 语法检查（裸 `--name` 直接当 id）。
- **[36]/[45] 用户文档失效**：cli-reference 至少 6 处漂移（含不存在的 --reply-to）；README 快速上手第一条命令 `lyre send leader` 必然失败（seed 的是 dispatcher）。
- **[43] 三个 subprocess 测试用"恢复 env"之名批量删除 os.environ**（confirmed）——会话级污染，潜在顺序依赖炸弹。

### 1.4 完备性批判指出的盲区（本轮未覆盖、结论可能受影响）

1. **拔线窗口未系统枚举**：review 证明了 Step-9 单提交点模型已死（mail 在 wakeup 中途即持久提交），但没有为多提交点现实重做 kill-point 分析。已知一个未列入 findings 的后果：恢复重跑会以新 external_id 重发邮件（at-least-once 语义，owner 会看到重复信）。
2. **安全姿态未整体评估**：散点 findings 之外，"模型可提取凭据 + 明文 transcript 永存 + 不可信内容→富工具 agent 的注入链"作为整体从未被审视。
3. **运维故事缺位**：`src/lyre` 全库无任何 logging 配置（grep 零命中）——所有"仅日志可见"的失败路径实际是**不可见**；无备份故事（而 schema 策略是"nuke the DB"，DB 里有 owner 的全部邮件正史）；tasks/mailbox_messages/blobs/transcripts 四个存储无界增长。
4. **成本可观测性 vs "不设硬预算"约束**：owner 的既定约束是"无 $ 预算、靠停住 runaway"，但当前连**看见** runaway 都做不到——cost 字段死、compaction/summary 的二次 LLM 调用不计量、无 per-task 归因。
5. **零真实 provider 执行**：全部 adapter findings 都是 code-read 所得（也恰是离线套件结构性测不到的一类）；建议一次 per-registry-entry 的真 key 冒烟矩阵。
6. **时区**：future_mail 全 UTC 且无 owner 时区概念——对 UTC+8 的 owner，"每天 9 点提醒"会在 17 点响。

---

## 2. 理念 Review

### 2.1 五铁律逐条裁定（五法审计 agent，全库 grep+read 验证）

| 铁律 | 裁定 | 要点 |
|---|---|---|
| 一 Provider 中立 | **成立（带例外）** | import seam 双向干净；例外：adapter_factory 的 provider→class 映射在 adapter/ 之外硬编码（"一个模块+一条注册"的口号低估了真实接缝）；LYRE_DEFAULT_MODEL fallback 是死文档。 |
| 二 Lyre 网关 | **成立（带例外）——但正典定义自相矛盾** | 机械上只有一个效果通道（_dispatch_tool 双重 allowlist）。但 FOUNDATION §3.3"定论"版说 shell 不走网关、持久层有 Unix-socket 强隔离（"agent 拿不到持久层连接|强"）——后者从未建成，mcp_server/ 是空壳；实际任何持 shell_exec 的 persona 都能 `sqlite3 ~/.lyre/lyre.db` 直写他人邮箱（HOME 在 allowlist 里）。新文档接受了这个现实，正典从未修订。 |
| 三 拔线测试 | **成立（带例外）** | 主干真实：租约恢复、幂等 outbox、mail-before-flip、原子写规约、真 SIGKILL chaos 测试。两个真洞即 C-1/C-2；另外 TRANSACTION_BOUNDARIES 的 kill-point 表已不描述系统（实况**比文档更持久**——mail 中途即提交——但正典恢复分析失效，且引入了文档未承认的 at-least-once 重发语义）。 |
| 四 三档持久层 | **成立** | checkpoint fenced 写入并回注；冷档运行时只写不读（只有 dashboard/CLI 观测读）。 |
| 五 Mailbox 唯一原语 | **成立** | 所有 agent 发信走 outbox→dispatcher；直接 insert_message 的调用方全是被认可的边缘客户端（CLI/dashboard/Lark/系统邮件）。近违例：shell_exec 的 DB 后门（同铁律二）；metadata.kind 的类型化系统邮件悄然回归（正典明文"没有 type"）。 |

### 2.2 大赌注的盈亏判定（理念 agent + 我的综合）

- **Mailbox-only：赢，且赢法漂亮。** fan-in barrier 数的是已投递的 mail 行、threads 是信封元数据、有界循环是带预算的 recurring 自邮件、监督升级是幂等邮件、Lark 通道只是又一个 mailbox 客户端——五个后续设计全部由同一原语组合而成，没有一个需要新通讯机制。这是架构核心资产，**应继续加倍下注**。
- **Kill-test 作为判据：赢，但需要重新立约。** 文化是真的（fsync 注释引铁律、chaos 测试、mail-before-flip）。但奠基契约（"Step 1-8 任意 kill = 本次唤醒没发生过"）已被实现演化否定：现实是"每个效果性工具调用即时持久 + 幂等 + 末尾 fenced finalize"。这**不是更弱**的模型（某些方面更强），但它没有被任何文档承认，于是两个 critical 都恰好藏在新旧模型的缝里。需要一份新的事务边界正典。
- **Stateless wakeup：代价高昂但正确。** 为对抗模型失忆已付出三轮补偿（push-context、compaction 加固、scratchpad/notes/threads）；42KB 的 barrier 设计文档只为替换一个 `await`。但换来的是 provider 热切换、水平扩展、以及整个 kill-safe 故事的成立。代价已付清，回头反而亏。
- **Provider 中立：赢。** 代码层真实成立。但注意：诚实性 bug（stop_reason 擦除、thinking 不启用）说明"中立"目前停在语法层——**语义中立**（各 provider 下行为等价）还需要 error taxonomy 与真机冒烟矩阵。
- **与"Claude Code in a loop"的差异化：新文档诚实。** CAPABILITY_DISCOVERY 承认自研 coding loop 打不过专业 coding agent，把 Lyre 重新定位为 kill-safe 的持久组织层（mailbox、租约、监督、owner 异步 UX），编码本身外包。这是战略上正确的自我认知，应在 FOUNDATION 层面正式化。

### 2.3 正典的病理：append-only 文档堆积

18 份设计文档中 7 份是 PR-round changelog；理解今日调度器要在过时基底上心算重放 ≥6 份文档；调度器相位编号（-1, 0.5, 0, 0.7, 0.8, 2, 3, 4——没有 Phase 1）是同一堆积的代码化石。FOUNDATION §7 导航只列 8/18。新文档有 `实现修正` 横幅规约，奠基文档没有任何 supersession 标记，而 CLAUDE.md 还**强制要求**动 agent_loop/persistence 前先读它们——等于强制喂错误模型。运行时给自己的上下文做 compaction，文档集需要同样的纪律。

### 2.4 理念层缺口（与其说缺陷不如说未完成的思考）

1. **"无预算、停 runaway"约束目前不可执行**：H2（跨 wakeup 无进展闸）设计已定却被跳过（owner 排序 D1→C4→O1→O2→H2→O3 中唯一未执行项）；加上 [8] 的无界重启循环和成本不可见，anti-runaway 故事三处漏风。
2. **类型化系统邮件已事实回归**（metadata.kind / fan_in / thread_id / auto_dispatched 被运行时路由依赖），与"没有 type，自然语言足够"的正典条款未对账。要么修订正典承认一个受约束的系统元数据协议，要么收缩用法。
3. **Owner 时区、备份、日志**：把 owner 当"背后有人的 actor"的哲学很好，但这个 actor 生活在 UTC+8、会换机器、需要在系统出错时看得见——三件事都还没被当作一等需求。

---

## 3. 熵减计划

按"先止血、再对正典、再删死物、再收敛重复、最后拆模块"排序。每批 ≈ 一个 PR，沿用项目自己的 doc-first 方法。验收一律：ruff + mypy + 全测试绿，行为变化配测试。

### E0 止血（✅ 已实现，2026-06-10；经 4-lens 对抗 review + 验证后落地）
1. ✅ C-1：`wakeups.start` + `claim_lease` 合并为单个 `repos.transaction()`（比 claim-before-insert 更强：窗口直接消失；丢失竞争经 `_LeaseUnclaimed` 回滚至零痕迹）+ 崩溃窗口回归测试。
2. ✅ C-2：新建 `src/lyre/fsutil.py::atomic_write_text`（mkstemp+fsync+replace，全库最强形态）；update_scratchpad / wakeup_summary / fs_personas / seed notes 模板四处收敛 + 中断写测试。
3. ✅ [20] stop_reason：message_delta 时 stash、MessageStop 单点发射、`_STOP_REASON_MAP` 映射（含 review 补抓的 `model_context_window_exceeded`→max_tokens）+ 测试。
4. ✅ [21] 熔断器 half_open 失败重开电路（单次探测失败即重开）+ 测试。
5. ✅ [4]/[27] flatten SSOT 进 `identity.py`（`flat_id()`/`agent_notes_rel_path()`），五个调用点全换；stray 文件一次性合并（对抗 review 抓出 blocker 后改为 write-then-unlink + marker 幂等 + errors="replace"）+ 6 个新测试。
验收：900 tests / ruff / mypy strict 全绿（基线 884，+16 新测试）。对抗 review 的遗留递延项：inline-serve 共享连接回滚危害（并入 F3 既有项）、fsutil tmp 残渣/NAME_MAX/0600 三个 nit、事务内 chaos kill point 测试升级。

### E1 正典对齐（文档 compaction，1-2 个 PR，高杠杆低风险）
1. **CLAUDE.md 重写调度器/任务段**：真实相位表（-1 / 0.5 / 0 / 0.7-inert / 0.8 / 2 / 3 / 4）、fan-in 邮件驱动同步（不是 parent-resume）、needs_input 标注为 reserved-dormant；修正 "~15s, 350+ tests"、configuration.md "full list" 等失效声明。
2. **四份奠基文档加 status 横幅**（一段英文即可，遵守"不翻译"规约）：FOUNDATION §3.3 的网关强隔离声明、AGENT_CONTRACT §4.4、AGENT_RUNTIME §2-4 的 subprocess/MCP 架构、TRANSACTION_BOUNDARIES 的 Step-9 模型——标明"superseded by current implementation, see X"。同时修正 GH_TOKEN 清洗的虚假安全声明（[49]）。
3. **写一份 living 的 `RUNTIME_CURRENT.md`**（或重写 AGENT_RUNTIME）：吸收 6 份 round-doc 的结论，描述今天的 wakeup 生命周期、真实事务模型（即时持久 + 幂等 + fenced finalize + at-least-once 邮件）、调度相位。给所有设计文档加 settled/living/historical 三态头；FOUNDATION §7 导航补全 18 份。
4. **用户文档止损**：README/getting-started/concepts 的 `leader`→`dispatcher` 全量替换（[45]）；cli-reference 从 `lyre --help` 再生 + 一个 doc-smoke 测试（quick-start 的 send 目标必须存在于 seed 集）。
5. PERSONAS.md / PERSISTENCE_SCHEMA.md / DASHBOARD.md：重写或盖 historical 戳（[50]/[40]）。AGENT_THREADS / ORCHESTRATION_ROBUSTNESS 的状态行已落地却写着"待实现"（[133]/[134]）——顺手更新。

### E2 死物清除（1 个 PR，全部 grep 验证零调用者）
- `src/lyre/mcp_server/` 空包；skills/artifacts/local_hot 三套 table+repo+Protocol+model（单文件迁移规约使删除廉价，有先例 commit）；StreamError；identity.validate_agent_id/is_bare_id；blocker_watcher shim（3 个测试文件改 import MailWatcher）。
- 死 Config 字段：anthropic_api_key / anthropic_base_url / env_path（20+ 测试文件的样板随之瘦身）。
- 列保留裁决：fan_in.budget_tokens / dry_round、tasks.deadline——按 observed-not-theoretical 原则要么删、要么在 roadmap 里写明激活计划（tier_overrides 有被激活的先例，所以这是真决策不是橡皮章）。
- 清理 .git 里被否决/被取代的陈旧分支（保留 spec/plugin-system 待 §4 决策）。

### E3 重复收敛（1-2 个 PR）
1. **agent 创建四路归一**：`ensure_agent(repos, persona, name, parent)` 服务函数（落 runtime/identity.py 或新 runtime/agent_admin.py），approved+singleton+grammar 三检查统一；CLI send / CLI agent-create / dashboard / create_agent 工具全部改调（[58]，顺带消灭 `_Stub` hack）。
2. scheduled-mail 校验/派生管线：mailbox 工具与 CLI 各 ~90 行重复 → 抽到 future_mail.py 旁（[139]）。
3. 三个 YAML frontmatter 解析器收敛为一（persona 那个会因 owner 手滑的坏 YAML 炸 serve——收敛时顺手 fail-soft）（[143]）。
4. urgency 词表/秩映射唯一化（7 处枚举、2 处 verbatim 重复 dict）（[144]）。

### E4 God-module 拆分（2-3 个 PR，纯机械，放最后做以免与上面冲突）
1. **main.py（2093 行，19 份连接样板）**→ `cli/` 包（mail/agents/inspect/serve/onboard 分组）+ `open_repos()` async context manager；inline wakeups SQL 移入 DAO（顺带让 audit/tail 的"CLI 旁路是否 sanctioned"有了答案：不再旁路）。
2. **scheduler.py（1959 行）**：tick-policy 与 wakeup-execution engine（1358-1958 的可分离 600 行）拆两个模块。
3. **agent_loop.run()（607 行单方法）**：per-turn policy 块（nudge / dead-loop / compaction 触发）提取为私有方法。
4. sqlite_impl.py 不动（熵审计：大而规整，无需拆）。

### E5 配置面收口（半个 PR）
- auto_wake_on_mail、default_dashboard_port：接线或删除（[59]/[34]）；LYRE_DEFAULT_MODEL：实现 fallback 或删旋钮（[57]，二选一明确记录）。
- .env 优先级：定一个方向（建议 CWD-wins 以兑现 "dev override"），改代码或改 docstring + 加顺序测试（[32]）。
- [[models]] replace-all 语义：保留行为、修三处文档（[33]）；re-onboard 的 [[models]] 字段保真（[31]）。
- configuration.md 补齐 27 个 LYRE_* 变量 + 一个 docs-vs-code 的 grep 测试冻结基线（[138]）。

---

## 4. 功能迭代计划

排序原则：先完成 owner 已承诺的安全网（H2），再补"看得见"（成本/日志），再修编排语义，然后是安全与运维成熟度，最后是战略扩展。

### F1 H2：跨 wakeup 无进展闸（owner 排序中唯一被跳过的项）
设计决策已全部定稿（LONG_RUNNING_ROBUSTNESS_3 §9：work-AND-no-output 判据；max_no_progress 默认 0/建议 3）。实现：scheduled_mail.no_progress_count + Phase −1 按 thread_id JOIN wakeups 的进展检查。同 PR 顺带修编排簇的烧钱循环（[8]：接受 quorum_met/expired 组的晚到结果或跳过 O2 降级；ephemeral 重启强度改累计上限）——两者合起来才算把"无预算、靠停"的约束真正闭环。

### F2 成本与运行可观测性（回应完备性批判 #3/#4）
1. 计量补全：compaction/summary 的二次 LLM 调用入账 wakeup 计量；per-task/per-thread token 归因；dashboard 加 spend 面板（沿用 wakeups 表既有列，激活死掉的 cost 字段或删除）。
2. **日志配置**：structlog 输出到滚动文件（~/.lyre/logs/），serve 启动时配置——否则全库几十处"日志可见"的失败路径都是黑洞。
3. 留存与备份：maintenance 扩展到 tasks（与"completed-task population grows"的索引注释对账）；mailbox_messages 的法务级保留策略明确化（铁律五正史 vs 无界增长）；写一页 backup/restore 文档（DB+object_store+memory 三件套快照）。
4. [12] 调度相位逐行错误隔离 + 毒行隔离阈值（镜像 outbox dispatcher 既有模式）。

### F3 编排语义补完（[7]/[9]/[10]/[11] 簇）
- subprocess 模式下 dispatch 即关门（parent 先 claim+插 wakeup 行再 spawn，或 _active_subprocesses 按 agent 跟踪）；Phase 2 补 per-agent 门。
- 重启 one-for-one 化（复制完整 TaskSpec）；三条失败路径统一走"关 wakeup 行 + fail + task_terminated"单事务 helper。
- fan-in roster 跟随重启任务；O1 哨兵尊重重启政策。
- 共享连接事务隔离（[13]/[16]/[54]）：建议给 OutboxDispatcher 独立连接（WAL 本就支持多连接写者），并把 commit-lock 协议升格为 Repositories Protocol 的文档化不变量。

### F4 安全姿态轮（一份小设计文档 + 1-2 个 PR）
1. 把"模型可经 `env` 提取 credential bundle + 明文落 transcript"升格为显式决策：要么接受并写入 CAPABILITY_DISCOVERY 的威胁模型，要么做 transcript 侧 secret 掩码 + bundle 注入改 stdin/临时文件。
2. CSRF token（DASHBOARD.md 本来就承诺了，[38]）+ 非 loopback bind 的告警/令牌门（dashboard 与 Lark webhook 同步考虑）。
3. Lark `_handle_inbound` 测试矩阵（未授权丢弃/群聊丢弃/dedup/线程路由/urgency 前缀，全部依赖已有离线 fakes，[39]）；[37] 先订阅后 catch_up。
4. 工具治理小修：[25] 只对 self 收件箱 auto-mark；[26] cancel/list_scheduled_mail 加所有权（owner/dispatcher 豁免）。

### F5 Provider 语义中立轮
1. error taxonomy 落地（FAILURE_ROBUSTNESS 的既定 deferral）：StreamError 要么按设计激活（permanent 4xx 不烧 failover、auth 失败有别于 overload），要么删（E2 已删的话此处重建）。
2. thinking 战略统一：Anthropic 条目要么真启用 extended thinking（registry 驱动 budget_tokens）要么摘 `reasoning` 标签；fallback 时按目标模型剥离异源 thinking blocks（镜像 _strip_vision_blocks，[6]/[24]）；Responses adapter 的 reasoning 回放路径或文档化局限（[23]）。
3. **真机冒烟矩阵**：`lyre smoke` 命令——对每个 enabled registry 条目跑一次单 wakeup（强制工具调用 + 引发 max_tokens + thinking + 低压缩阈值），人工触发、有 key 才跑。这是离线套件结构性测不到的一整类 bug 的唯一补法。
4. AnthropicAdapter mock-stream 测试补齐到 OpenAI 同等水平（[42]）；loop 级 compaction 触发/thrash-bail 测试（[41]，fake_entry 加 context_window）。

### F6 Owner 体验轮（OWNER_AS_CHAT_PARTNER 既有 backlog + 时区）
- owner 时区进 config/user.md，future_mail 按 owner 本地时间解释 `--at 9:00`/cron（对 UTC+8 owner 这是日用刚需）。
- 既有 backlog：worker→owner blocker 的线程路由（经 dispatcher forward）、离线 digest、Lark inbound reactions。
- [5] thrash-bail 走 S0 缝（needs_continuation + task_terminated 可见性），道歉邮件只发给未被回复的 asker。

### F7 战略决策点（✅ 已全部由 owner 拍板，2026-06-10）
1. **plugin-system 分支** → **并入 main 但标 parked**：spec 文档合进 docs/design/ 盖 status: proposed/parked + 书面复活条件，从 main 可见但不排进当前实现计划。E1 给 FOUNDATION 写横幅时措辞为"gateway superseded; may revive via the plugin arc if endorsed"。
2. **类型化系统邮件** → **入正典，划清边界**：修订 FOUNDATION——原"no type"条款管 agent↔agent 语义层（仍然成立）；新增一小节"系统元数据协议"：metadata 下 kind/fan_in/thread_id/auto_dispatched 等为 runtime 保留命名空间，仅系统生成邮件可路由，agent 永远不需要自己读写。实现零改动。
3. **O3b 有界自动续跑** → **维持 deferral**：遵守 observed-not-theoretical 纪律；H2 先落地。复活条件保持原文（观察到"budget 提升仍不够且任务确在进展"的案例）。

### 建议节奏

```
E0（止血）→ E1（正典）→ F1（H2+烧钱循环）→ F2（可观测）
→ E2+E5（删死物/收配置）→ F3（编排）→ F4（安全）
→ E3（收敛）→ F5（provider）→ E4（拆分）→ F6（owner UX）
（F7 三个决策点尽早拍板，影响 E1/E2 的写法）
```

逻辑：正典先于一切重构（否则每个 PR 都在和错误文档打架）；H2 与可观测性先于编排细节（先能看见、能停住）；纯机械拆分放最后（避免 rebase 地狱）。

---

## 附录：验证状态说明

- 145 findings：2 critical（均 confirmed）、63 major（45 confirmed / 15 partially-correct / 3 因验证容量上限未过验证，其中 [62][63] 两条 CLAUDE.md/AGENT_RUNTIME 文档漂移已由综合者直接读文档证实）、80 minor（未做对抗验证，按线索使用）。
- 0 条被驳回；15 条 partial 的更正已并入上文表述（最重要的三条：共享连接回滚 bug 真实但可达面窄；thinking-fallback 真实但触达条件比原述窄；maintenance commit-lock 旁路今天不可达属潜伏雷）。
- 本轮纯静态：未运行系统、未接真实 provider——完备性批判 §1.4 列出的六个盲区即由此而来，其中 #1（拔线窗口重枚举）建议作为 E1 正典重写的一部分完成。
