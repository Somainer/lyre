# Lyre — Dashboard 设计

> **文档定位**：定义 owner 与 Lyre 交互的 web UI——FastAPI + HTMX + SSE 服务端渲染；半交互（回 blocker / 审 approvals / 派 task）。
> **相关**：[`FOUNDATION.md §3.6`](./FOUNDATION.md#36-铁律五mailbox-是-lyre-通讯的唯一原语)（Inbox/Dashboard 是同一 mailbox 的两种视图）；[`FOUNDATION.md §3.7`](./FOUNDATION.md#37-五层架构整体分层)（第 5 层：观测）；[`AGENT_CONTRACT.md`](./AGENT_CONTRACT.md)；[`PERSISTENCE_SCHEMA.md`](./PERSISTENCE_SCHEMA.md)；[`AGENT_RUNTIME.md`](./AGENT_RUNTIME.md)；[`PERSONAS.md`](./PERSONAS.md)。
>
> **澄清术语**：
> - **Mailbox dashboard view**（铁律五）= 同一 mailbox 按 `urgency < high` 过滤的浏览视图；只是查询 filter
> - **Dashboard**（本文）= owner 的整体观测面板，**包含** mailbox 视图作为其中两个 tab（Inbox / Feed）

---

## 目录

1. [整体形态与原则](#1-整体形态与原则)
2. [信息架构（6 tab）](#2-信息架构6-tab)
3. [路由与 API](#3-路由与-api)
4. [SSE 推送实现](#4-sse-推送实现)
5. [数据查询模式（DAO 增补）](#5-数据查询模式dao-增补)
6. [HTMX 模板片段示例](#6-htmx-模板片段示例)
7. [部署、端口、配置](#7-部署端口配置)
8. [已识别但待解决的问题](#8-已识别但待解决的问题)

---

## 1. 整体形态与原则

| 维度 | 决定 |
|---|---|
| 技术栈 | **FastAPI + Jinja2 + HTMX + SSE**，服务端渲染 HTML，按需 partial 替换 |
| 交互性 | **半交互**：mailbox 回 blocker / 审 skill+persona 草案 / 派新 task / 停某任务。**不**改 persona prompt / Tier 矩阵 / model routing 等结构性配置 |
| 实时性 | mailbox 走 **SSE**；tasks / wakeups / approvals 走 5-10s **polling** |
| 多用户 | 单 owner 单 localhost，**无 auth**；绑 `127.0.0.1` |
| 进程拓扑 | Lyre 主进程内嵌 uvicorn（asyncio 同 loop 跑），跟 scheduler / outbox dispatcher 共生死 |

**与 Lyre 已有 CLI 的关系**：

- `lyre status` / `lyre mailbox` 等 CLI 命令**保留**——dashboard 是补充不是替代
- dashboard 不引入新的状态机；它只是 DAO 已有数据的 read-mostly 视图 + 几个写入 endpoint

**风格**：

- 单页应用感（HTMX 替换 partial 不全页 reload）但**无 SPA 复杂度**
- 字体小、信息密集、像 trading terminal——owner 想一眼看到所有 in-flight
- 暗色优先，跟 Lyre 自身气质（基础设施工具）匹配

---

## 2. 信息架构（6 tab）

### v0.1 必有

| Tab | 路径 | 内容 | 主要数据源 | 更新方式 |
|---|---|---|---|---|
| **Home** | `/` | 今日卡片：in-progress 任务数、blocker 数、24h 完成数、24h token 用量；最近 5 条 blocker 速览 | `tasks` + `wakeups` + `mailbox_messages` | SSE（mailbox 部分）+ polling 5s（卡片数字） |
| **Activity** | `/activity` | 全局 chat-bubble 流（按时序，旧→新）：mail（markdown 渲染 + urgency 配色）/ wakeup_end（带 ctx % + compact ×N 徽章）/ task 转变（scheduler 的 auto-wake "Check inbox" 任务被自动过滤）/ silent_close 兜底 note。`<details>` open state 跨 htmx swap 保留；sticky-bottom 自动滚 | `tasks` + `wakeups` + `mailbox_messages` | polling 2s |
| **Agents** | `/agents` / `/agents/<id>` | 列出所有 agent；点击进单 agent timeline：含 thinking blocks（紫色虚线 🧠）、tool calls（折叠完整 args）、mail in/out、wakeup_end | 同 activity + transcript jsonl tail | polling 3s |
| **Inbox** | `/inbox` | owner mailbox（默认全 urgency，可 `?urgency=high+` 筛选）；mail body 走 markdown 渲染 | `mailbox_messages` WHERE recipient='owner' | SSE |
| **Feed** | `/feed` | owner mailbox 全量；时间倒序；筛选条 | `mailbox_messages` WHERE recipient='owner' | SSE |
| **Tasks** | `/tasks` | 任务列表 + 状态 + parent/child 关系树；点 task 进 `/tasks/{id}` 看详情 | `tasks` | polling 10s |
| **Wakeups** | `/wakeups` | 最近 N 次唤醒 + metering（token / wall_ms / provider / model / fallback / **context_peak_tokens / compaction_count**）；点开看 transcript | `wakeups` + object_store transcript.jsonl | polling 10s |
| **Approvals** | `/approvals` | 待审 skills（status='proposed'）+ 待审 personas（status='proposed'）；同一页两个 section | `skills` / `personas` | polling 10s |

### 后续加（不在 dashboard v0.1）

| Tab | 触发时机 |
|---|---|
| **Costs** | Q7 解冻——预算控制纳入 MVP 之后 |
| **Personas** | 当前 personas 表清单 + 各 persona 的 system_prompt 预览（编辑由文件直接做：仓库内 shipped 或 ~/.lyre/personas/<name>.md 覆盖）|
| **Skills** | 已批准 / 已弃用列表 + diff 查看 |
| **Model Health** | Q9 引入的 HealthTracker / Router 状态可视化（断路器状态、最近 5min 失败率） |

---

## 3. 路由与 API

```
HTML 页面（HTMX 友好）
  GET  /                   → Home
  GET  /inbox              → Inbox（urgency>=high）
  GET  /feed               → Feed（全量）
  GET  /tasks              → Tasks 列表
  GET  /tasks/{id}         → Task 详情（含 children + 最近 wakeups）
  GET  /wakeups            → Wakeups 列表
  GET  /wakeups/{id}       → Wakeup 详情 + transcript viewer
  GET  /approvals          → Approvals（skills + personas 两 section）

HTMX partials（局部刷新用）
  GET  /partials/home/cards         → 卡片数字
  GET  /partials/inbox/items?since= → 增量 mailbox items
  GET  /partials/tasks/row/{id}     → 单行 task（状态变化时替换）

写入 endpoints（HTMX POST 后服务器返回 200 + 新 HTML partial）
  POST /mailbox/reply         body={parent_msg_id, urgency, body}
  POST /approvals/skill/{id}  body={status, comment}
  POST /approvals/persona/{name} body={status, comment}
  POST /dispatch              body={persona, goal, acceptance}
  POST /tasks/{id}/cancel
  POST /tasks/{id}/stop       → 给该任务的 wakeup 发 urgency=blocker

SSE
  GET  /sse/mailbox       → 服务器推送新 mailbox 消息（owner 的）
```

**约定**：

- 写入 endpoint 不返回 JSON，返回**渲染好的 HTML partial**（HTMX 直接 swap）
- 错误用 `4xx` 状态码 + 服务端 partial 包错误信息
- 所有 `POST` 自动加 CSRF token（HTMX 的 hx-headers）

---

## 4. SSE 推送实现

### 服务端 (FastAPI)

```python
# src/lyre/dashboard/sse.py

class MailboxBroadcaster:
    """In-process pub-sub. Push from MailboxRepository.insert_message;
    fanout to all connected EventSource clients."""

    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    async def publish(self, msg: MailboxMessage) -> None:
        # Fire-and-forget; if queue full, drop oldest
        for q in list(self._subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                _ = q.get_nowait()
                q.put_nowait(msg)


@router.get("/sse/mailbox")
async def sse_mailbox(request: Request, recipient: str = "owner"):
    """Stream new messages to client. Use EventSource on the browser side."""
    queue = broadcaster.subscribe()
    async def event_stream():
        try:
            while not await request.is_disconnected():
                msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                if msg.recipient == recipient:
                    html = render_template("partials/mailbox_item.html", msg=msg)
                    yield f"event: mailbox\ndata: {html}\n\n"
        except asyncio.TimeoutError:
            yield ": keepalive\n\n"
        finally:
            broadcaster.unsubscribe(queue)
    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

### 触发点

`MailboxRepository.insert_message` 或 `OutboxDispatcher` 投递成功后调 `broadcaster.publish(msg)`。

**关键**：broadcaster 是**同进程**广播；当未来切到多进程时需换成 Postgres LISTEN/NOTIFY 或 Redis pub/sub。MVP 单进程不打紧。

### 客户端 (HTMX + 原生 EventSource)

```html
<div id="inbox-items">
  {% for msg in initial_items %}
    {% include "partials/mailbox_item.html" %}
  {% endfor %}
</div>

<script>
  const es = new EventSource('/sse/mailbox?recipient=owner');
  es.addEventListener('mailbox', (e) => {
    document.getElementById('inbox-items').insertAdjacentHTML('afterbegin', e.data);
  });
</script>
```

HTMX 的 `hx-sse` 也能直接接 SSE，但用原生 EventSource 更可控。

---

## 5. 数据查询模式（DAO 增补）

Dashboard 大部分查询复用已有 DAO method。需要新增的：

| 新方法 | 所在 Repository | 用途 |
|---|---|---|
| `read_messages_paged(recipient, before_id, limit)` | MailboxRepository | Feed 倒序分页 |
| `read_inbox_grouped_by_task(recipient)` | MailboxRepository | Inbox 按 task 分组渲染 |
| `count_unread_blockers(recipient)` | MailboxRepository | Home 卡片数字 |
| `find_recent(limit, status_filter=None)` | TaskRepository | Tasks 列表 |
| `find_tree(root_task_id)` | TaskRepository | 任务树（含子）|
| `list_recent(limit, since=None)` | WakeupRepository | Wakeups 列表 |
| `count_completed_since(since)` / `sum_tokens_since(since)` | TaskRepository / WakeupRepository | Home 24h 数字 |
| `list_proposed()` | SkillRepository / PersonaRepository | Approvals 列表 |
| `read_transcript(wakeup_id, offset=0, limit=N)` | ObjectStore | Wakeup 详情 transcript viewer（流式）|

**约定**：

- Dashboard 查询走**read-only** 路径，不进 outbox / 不动 lease
- 写入 endpoint 走**已有**写入路径：例如 `/mailbox/reply` 调 `mailbox.insert_message`，不绕路
- 所有跨实体查询保持单一表查询；JOIN 留在 Python 端组装（SQLite 也能做但便于 PG migration）

---

## 6. HTMX 模板片段示例

### Tasks 行（轮询替换）

```html
<!-- templates/partials/task_row.html -->
<tr id="task-{{ task.id }}"
    hx-get="/partials/tasks/row/{{ task.id }}"
    hx-trigger="every 10s"
    hx-swap="outerHTML">
  <td><code>{{ task.id[:8] }}</code></td>
  <td>{{ task.persona_name }}</td>
  <td>
    <span class="status status-{{ task.status }}">{{ task.status }}</span>
  </td>
  <td>{{ task.goal[:60] }}</td>
  <td>
    <a href="/tasks/{{ task.id }}" hx-boost="true">详情</a>
    {% if task.status == 'in_progress' %}
      <button hx-post="/tasks/{{ task.id }}/stop"
              hx-confirm="向该 task 发 blocker 让其停下？">停</button>
    {% endif %}
  </td>
</tr>
```

### Inbox 回复表单

```html
<!-- templates/partials/inbox_reply.html -->
<form hx-post="/mailbox/reply"
      hx-target="#mailbox-thread-{{ msg.id }}"
      hx-swap="afterend">
  <input type="hidden" name="parent_msg_id" value="{{ msg.id }}">
  <select name="urgency">
    <option value="high" selected>high</option>
    <option value="normal">normal</option>
    <option value="blocker">blocker</option>
  </select>
  <textarea name="body" rows="3" autofocus></textarea>
  <button type="submit">回复</button>
</form>
```

### Approval 处理

```html
<!-- templates/partials/approval_skill.html -->
<div id="approval-skill-{{ skill.id }}" class="approval">
  <h3>{{ skill.name }}</h3>
  <p>{{ skill.frontmatter.description }}</p>
  <details>
    <summary>查看完整 body</summary>
    <pre>{{ skill.body }}</pre>
  </details>
  <form hx-post="/approvals/skill/{{ skill.id }}"
        hx-target="#approval-skill-{{ skill.id }}"
        hx-swap="outerHTML">
    <input type="hidden" name="status">
    <button type="submit" name="status" value="approved">approve</button>
    <button type="submit" name="status" value="rejected">reject</button>
  </form>
</div>
```

---

## 7. 部署、端口、配置

| 配置 | 默认 | env 覆盖 |
|---|---|---|
| 端口 | `8765` | `LYRE_DASHBOARD_PORT` |
| 绑定地址 | `127.0.0.1` | `LYRE_DASHBOARD_HOST`（**不**建议改成 0.0.0.0，无 auth）|
| 启用 | 默认 enabled | `LYRE_DASHBOARD_ENABLED=0` 关掉 |

**进程拓扑**：

```
lyre serve
  └── asyncio main loop
      ├── Scheduler.run()
      ├── OutboxDispatcher.run()
      └── uvicorn.serve(dashboard_app)
```

三者并行 asyncio task，共享同一个 `SqliteRepositories` 连接（SQLite WAL 模式支持单进程多 coroutine 读写）。

**容器化兼容**：

- 你已经设计了"整个 Lyre 装进 Docker"作为 OS 级隔离 envelope（AGENT_CONTRACT §4.2）
- Docker 启动时：`docker run -p 8765:8765 lyre`，浏览器照常访问 `http://localhost:8765`
- 跟 Lyre 主代码零耦合

**CLI 子命令**（不在 v0.1 必有，但顺便加）：

```bash
lyre dashboard                 # 启 dashboard（独立模式，前台跑；不带 scheduler）
lyre serve --no-dashboard      # 跑 scheduler 但不启 dashboard
```

---

## 8. 已识别但待解决的问题

1. **Static 资源 CDN vs vendor**：HTMX 走 `unpkg.com/htmx.org` 还是 vendor 到 `static/`？v0.1 倾向 vendor（离线可用、跨容器一致）
2. **SSE 在 Lyre 整体容器化下的反代**：如果 owner 在 Docker container 里跑 Lyre 又用 nginx 反代到外网，SSE 需要 `proxy_buffering off` 等配置；写到 ops doc
3. **Auth 是否真不要**：本地单 owner OK；但有 owner 在远端服务器跑 + ssh tunnel 转端口用——这种场景需要 token auth；v0.2 加可选 token
4. **Dashboard 自身的 transcript**：dashboard 收到的 HTTP 请求要不要 log 到 cold archive？v0.1 倾向只 log 写入 endpoint，read-only 不 log
5. **HTMX 写入的幂等性**：用户连点两次 approve，怎么办？v0.1 用 CSRF token + 服务端检查 status 已变化即返回 "已处理" 提示
6. **CSRF 实现细节**：单 owner localhost 严格来说不需要 CSRF；但 dashboard 跑在 8765 时，浏览器可能误信任其他本地服务。v0.1 上一个最简 token（cookie + form hidden）
7. **任务树渲染规模**：单 root 下子任务 > 100 时怎么折叠？v0.1 默认显示 top 20，"展开全部" 按需加载
8. **Transcript viewer 的体积**：长任务 transcript 可能数十 MB；v0.1 流式分页（首屏 1000 行，向下滚动 lazy load）
9. **Model Health 与 Q9 HealthTracker 集成时机**：Q9 已经在 runtime 引入 HealthTracker；dashboard tab 留到 Sprint D 后续，不在 v0.1
10. **多 owner / 多 dashboard 实例**：MVP 单 owner；将来 owner_id 字段加上后，dashboard 需要 login 选 owner

---

