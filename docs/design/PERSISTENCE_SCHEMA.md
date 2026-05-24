# Lyre — 持久层 Schema

> **文档定位**：定义 Lyre 持久层的物理实现、表 schema、并发与事务策略、后端可替换性。MVP 走 SQLite（runtime state）+ 文件系统（global = soul / memory / skills / personas / config）+ 本地文件系统（cold-archive object store）；保留 SQLite → Postgres 迁移路径作为 scaling 出口。**没有向量库**——facts / soul / skills 全是 markdown 文件，agent 用 grep / shell_exec 自管理。
> **相关**：[`FOUNDATION.md §3.5`](./FOUNDATION.md#35-铁律四持久层按作用域分三档) 三档持久层；[`FOUNDATION.md §3.7`](./FOUNDATION.md#37-五层架构整体分层) 五层架构第二层；[`AGENT_CONTRACT.md`](./AGENT_CONTRACT.md)；[`TRANSACTION_BOUNDARIES.md`](./TRANSACTION_BOUNDARIES.md)。

---

## 目录

1. [物理布局（4 个存储后端）](#1-物理布局4-个存储后端)
2. [三档持久层的物理映射](#2-三档持久层的物理映射)
3. [表 schema](#3-表-schema)
4. [SQL 后端抽象层（DAO Protocol + 双实现）](#4-sql-后端抽象层dao-protocol--双实现)
5. [并发与事务策略](#5-并发与事务策略)
6. [Object store 路径约定](#6-object-store-路径约定)
7. [Migration 路径（SQLite → Postgres）](#7-migration-路径sqlite--postgres)
8. [v0.1 已识别但待解决的问题](#8-v01-已识别但待解决的问题)

---

## 1. 物理布局（3 个存储后端）

| 存储 | 责任 | MVP 选型 | 后期升级 |
|---|---|---|---|
| **Git** | 代码与文档真相源 | git 本身 | 不变 |
| **关系数据库** | Runtime state（任务、mailbox、outbox、agents、wakeups、checkpoint、skills 元数据）| **SQLite**（单文件 `~/.lyre/lyre.db`）| Postgres |
| **文件系统** | Global 层全部内容：`~/.lyre/user.md`、`~/.lyre/memory/`、`~/.lyre/skills/`、`~/.lyre/personas/`、`~/.lyre/config.toml`；以及 cold-archive object store `~/.lyre/object_store/` | 直接读写文件 | S3 / MinIO 仅对象存储那部分 |

**选型理由**：

- **SQLite** 零运维（单文件、in-process、跨平台一致），匹配"裸 subprocess + tmpdir"哲学。MVP 单 owner 单机的 QPS 远低于 SQLite 上限。
- **文件系统作 global 层**：soul 是用户手写的、skills / facts 是 agent 直接 grep 维护的，没有跨索引语义查询的需求——所以不引入向量库；省一个组件，省一类一致性问题（DB row 与 embedding 漂移）。
- **本地文件系统作 object store** 避免引入 S3 依赖；切 S3 / MinIO 只换 `ObjectStore` 适配层。

切 Postgres 的信号（**不在 MVP**）：

- 任务量到 dispatcher 单进程吃不消（百千级 outbox/s）
- 需要多 region 部署
- 需要复杂分析查询（窗口函数、CTE 大量使用）
- 多用户 / 多 owner（Lyre 演化到平台化）

---

## 2. 三档持久层的物理映射

| 三档 | 内容 | 物理实现 |
|---|---|---|
| **Local-hot**（任务私有，[FOUNDATION §3.5](./FOUNDATION.md#35-铁律四持久层按作用域分三档)）| 进度状态机、中间推理、试错记录、临时草稿、读过的文件清单 | `tasks.checkpoint` JSON（覆盖语义）+ `local_hot` 键值表（大条目走 object store） |
| **Global**（生态公共，[FOUNDATION §3.8](./FOUNDATION.md#38-global-层的具体形态skills--user--memory-三类条目) 三类条目）| ① **User identity**（owner 偏好，user-only-write）② **Skills**（程序性配方）③ **Memory / Facts**（agent 自维护的知识）| `~/.lyre/user.md`、`~/.lyre/skills/{approved,proposed}/<name>/SKILL.md`、`~/.lyre/memory/facts/<topic>.md`；DB 里只有 `personas` / `skills` 元数据行 + `artifacts`（已提交版本），以及实际 merge 进 git 的代码/文档 |
| **Cold-archive**（已完成任务过程留存）| 完整 LLM transcript、每次唤醒的 metering、tool call 日志 | `wakeups` 表（元数据行）+ object store 里的 `wakeups/{wakeup_id}/` 子目录 |

**写入路径**（[FOUNDATION §3.5](./FOUNDATION.md#35-铁律四持久层按作用域分三档) + [§3.8](./FOUNDATION.md#38-global-层的具体形态skills--soul--memory-三类条目)）：

- **User identity**：`lyre onboard` 写出 `~/.lyre/user.md` 初始模板；之后只有 owner 自己编辑。Agent 永不写。
- **Skills 提案 → 审批**：agent `propose_skill` 写到 `skills/proposed/`，reviewer-skill persona 审定后移到 `skills/approved/`；DB 的 `skills` 表只跟踪元数据 / 状态。
- **Memory / facts**：agent 直接 `shell_exec` / `python_exec` / `read_memory` 操作 `~/.lyre/memory/` 下的 markdown 文件，没有审批，没有 DB row。

---

## 3. 表 schema

> SQLite DDL 为主，Postgres 等价处用 `-- PG: ...` 注释标注差异。
>
> 所有 UUID 在 SQLite 中用 TEXT 存（应用层生成 UUIDv7 字符串，时序友好、索引性能好）；Postgres 用 UUID 类型。
> 所有 JSON 在 SQLite 中用 TEXT 存（用 JSON1 扩展函数访问）；Postgres 用 JSONB。
> 所有时间戳在 SQLite 中用 TEXT 存（ISO 8601 UTC，如 `2026-05-16T10:30:00.123Z`）；Postgres 用 TIMESTAMPTZ。

### 3.1 调度组：`tasks` / `wakeups`

```sql
-- 任务：一等持久对象
CREATE TABLE tasks (
  id                TEXT PRIMARY KEY,                   -- UUIDv7
  parent_task_id    TEXT REFERENCES tasks(id),
  persona_name      TEXT NOT NULL REFERENCES personas(name),
  goal              TEXT NOT NULL,
  acceptance        TEXT NOT NULL,
  status            TEXT NOT NULL CHECK (status IN
                      ('pending','in_progress','needs_input','completed','failed','cancelled')),
  lease_duration_s  INTEGER NOT NULL DEFAULT 1800,      -- 30 min。PG: INTERVAL
  lease_holder      TEXT,                               -- 当前持 lease 的 wakeup_id
  lease_until       TEXT,                               -- ISO 8601 UTC。PG: TIMESTAMPTZ
  checkpoint        TEXT,                               -- JSON。PG: JSONB
  tier_overrides    TEXT,                               -- JSON
  deadline          TEXT,
  metadata          TEXT,                               -- JSON
  created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  completed_at      TEXT
);
CREATE INDEX tasks_status_lease ON tasks(status, lease_until);
CREATE INDEX tasks_parent ON tasks(parent_task_id);
-- PG: CREATE INDEX tasks_status_lease ON tasks(status, lease_until) WHERE status='in_progress';
--     SQLite 不支持 partial index 表达式简单条件（其实支持，但保险起见全索引）

-- 唤醒：每次 agent subprocess 启动一行；cold archive 入口
CREATE TABLE wakeups (
  id                    TEXT PRIMARY KEY,                    -- UUIDv7
  task_id               TEXT NOT NULL REFERENCES tasks(id),
  persona_name          TEXT NOT NULL REFERENCES personas(name),
  started_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ended_at              TEXT,
  end_status            TEXT,                                -- completed / failed / needs_continuation / silent_close
  token_input           INTEGER,
  token_output          INTEGER,
  wall_clock_ms         INTEGER,
  tool_call_count       INTEGER,
  provider              TEXT,
  model                 TEXT,
  failure_report        TEXT,                                -- JSON。status=failed 时填
  transcript_uri        TEXT,                                -- 指向 object store 的完整 LLM transcript
  -- Migration 0006: per-wakeup context-window telemetry
  context_peak_tokens   INTEGER,                             -- 任一 turn 见过的最大 input_tokens
  compaction_count      INTEGER NOT NULL DEFAULT 0           -- 该 wakeup 自动 compact 次数
);
CREATE INDEX wakeups_task ON wakeups(task_id, started_at);
```

> Migration 0006 added `context_peak_tokens` + `compaction_count`. Dashboard `/agents/<id>` 用前者算 "peak / window %"，后者超过 0 时显示 "compact ×N" 徽章。silent_close 是一种 end_status，代表 wakeup 运行到末尾但没向唤醒者回复——见 runtime/agent_loop.py 的兜底邮件路径。


### 3.2 Mailbox 组：`mailboxes` / `mailbox_messages` / `outbox`

```sql
-- 每个 actor 的 mailbox 元数据
-- Migration 0005 删掉了原来的 last_processed_msg_id 单调 cursor —— 改成
-- 每条消息自带 read_at（见 mailbox_messages.read_at）。metadata JSON 里
-- 仍存 scheduler 用的 last_auto_triggered_msg_id（Phase 0 防止重复 auto-
-- dispatch），跟 agent 的 read state 解耦。
CREATE TABLE mailboxes (
  recipient                TEXT PRIMARY KEY,            -- 见 §8 命名约定待决
  metadata                 TEXT                         -- JSON
);

-- 实际消息（dispatcher 已投递的；发送端先经 outbox）
CREATE TABLE mailbox_messages (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,    -- PG: BIGSERIAL
  recipient       TEXT NOT NULL REFERENCES mailboxes(recipient),
  external_id     TEXT NOT NULL,                        -- 来自 outbox.external_id
  sender          TEXT NOT NULL,
  urgency         TEXT NOT NULL CHECK (urgency IN ('blocker','high','normal','low')),
  title           TEXT,                                 -- 0005: 列表展示用（≤140 char）
  body            TEXT NOT NULL,                        -- 自由文本
  task_id         TEXT REFERENCES tasks(id),
  parent_msg_id   INTEGER REFERENCES mailbox_messages(id),
  broadcast_id    TEXT,                                 -- 0002: 一条 send fanout 多人时同组
  recipients_all  TEXT,                                 -- JSON list（broadcast 时全员）
  metadata        TEXT,                                 -- JSON
  delivered_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  read_at         TEXT,                                 -- 0005: NULL=unread；mailbox_read 自动写入
  UNIQUE (recipient, external_id)                       -- 接收端幂等
);
CREATE INDEX mailbox_messages_inbox ON mailbox_messages(recipient, urgency, id);
CREATE INDEX mailbox_messages_unread ON mailbox_messages(recipient, id) WHERE read_at IS NULL;
-- 0005: per-message read state 取代了 mailboxes.last_processed_msg_id。
-- mailbox_read 工具拉 unread + 立即写 read_at；mark_read 工具显式标记。

-- Outbox：跨存储事务 + 异步副作用派发
CREATE TABLE outbox (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT, -- PG: BIGSERIAL
  task_id            TEXT NOT NULL REFERENCES tasks(id),
  wakeup_id          TEXT NOT NULL REFERENCES wakeups(id),
  kind               TEXT NOT NULL CHECK (kind IN
                       ('mailbox_send','tier1_notification')),
  payload            TEXT NOT NULL,                     -- JSON
  external_id        TEXT NOT NULL,
  created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  dispatched_at      TEXT,
  dispatch_attempts  INTEGER NOT NULL DEFAULT 0,
  last_error         TEXT,
  UNIQUE (kind, external_id)                            -- 防 agent 重做造成重复 outbox 行
);
CREATE INDEX outbox_undispatched ON outbox(created_at);
-- PG: CREATE INDEX outbox_undispatched ON outbox(created_at) WHERE dispatched_at IS NULL;
```

### 3.3 Persona 配置：filesystem-only (no DB table)

Persona 定义不再有 DB 表。 SSOT 是 `~/.lyre/personas/<name>/identity.md`
（YAML frontmatter + 系统提示词正文），由
`FilesystemPersonaRepository` 直接 walk 该目录读取。

```
~/.lyre/personas/
├── leader/
│   ├── identity.md          # frontmatter (name/kind/allowed_lyre_tools/
│   │                        #              model_preference/status/...) + body
│   └── APPEND.md            # 可选：owner 注入的额外语气/风格
├── worker-maintainer/
│   └── identity.md
└── ...
```

迁移 `0009_drop_personas_table.sql` 删掉旧的 `personas` 表并把
`agents.persona_name` / `tasks.persona_name` / `wakeups.persona_name`
的 FK 也一并去掉（runtime 仍然用 persona_name 字符串去查文件，但 DB
不再约束它必须命中某一行）。

> **Owner identity 不在 DB**。`~/.lyre/user.md` 是用户独写的文件——
> 其他 persona 也是同款模式（owner-curated markdown，runtime 只读不
> 写）；agent 想记录跨 wakeup 信息就写自己的
> `~/.lyre/memory/facts/agent-<id>-notes.md`。

### 3.4 Memory 组：`local_hot` / `artifacts` / `skills`

```sql
-- Local-hot：任务私有热区
-- 小条目直接 JSON 字段；大条目走 object store
CREATE TABLE local_hot (
  task_id      TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  key          TEXT NOT NULL,
  value        TEXT,                                    -- JSON
  blob_uri     TEXT,                                    -- 大条目 object store URI
  updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (task_id, key)
);
-- 任务完成或废弃时 DELETE FROM local_hot WHERE task_id=... 由 ON DELETE CASCADE 触发

-- Global facts: 不在 DB——facts 是 ~/.lyre/memory/facts/<topic>.md 下的
-- markdown 文件，agent 用 grep / read_memory 自维护，没有审批，没有 row。

-- Artifacts：任务产出物指针（blob 本体在 object store）
CREATE TABLE artifacts (
  id             TEXT PRIMARY KEY,                      -- UUIDv7
  task_id        TEXT NOT NULL REFERENCES tasks(id),
  wakeup_id      TEXT NOT NULL REFERENCES wakeups(id),
  kind           TEXT NOT NULL,                         -- 'patch' / 'test_report' / 'design_doc' / ...
  content_hash   TEXT NOT NULL,                         -- sha256:abc... 既是去重键也是 object store 路径
  blob_uri       TEXT NOT NULL,                         -- object store full URI
  size_bytes     INTEGER,
  metadata       TEXT,                                  -- JSON
  created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (content_hash)
);
CREATE INDEX artifacts_task ON artifacts(task_id);

-- Skills：global 层的程序性配方（markdown + YAML frontmatter）
-- Hermes / Pi 对齐；详见 FOUNDATION §3.8 与 AGENT_CONTRACT §4.6
CREATE TABLE skills (
  id              TEXT PRIMARY KEY,                      -- UUIDv7
  name            TEXT NOT NULL UNIQUE,                  -- 'apply-dependency-upgrade' 等
  frontmatter     TEXT NOT NULL,                         -- JSON: {description, triggers, required_tools, scope, version}
  body            TEXT NOT NULL,                         -- markdown body
  status          TEXT NOT NULL CHECK (status IN ('proposed','approved','deprecated')),
  source_task_id  TEXT REFERENCES tasks(id),             -- propose_skill 自荐时填
  reviewer        TEXT,                                  -- 审定 persona name 或 'owner'
  reviewed_at     TEXT,
  scope           TEXT,                                  -- 冗余字段，方便索引（提取自 frontmatter.scope）
  metadata        TEXT,                                  -- JSON
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX skills_status ON skills(status);
CREATE INDEX skills_scope ON skills(scope, status);
```

> **Skill body 也不在 DB**：runtime 现在直接读 `~/.lyre/skills/{approved,proposed}/<name>/SKILL.md`。`skills` 表只保留元数据 / 状态用于审批流追踪和 dashboard 列表查询。

### 3.5 SQLite 初始化 PRAGMA

```sql
-- 必备
PRAGMA journal_mode = WAL;                              -- 读写并发
PRAGMA foreign_keys = ON;                               -- 默认关闭，必须打开
PRAGMA synchronous = NORMAL;                            -- WAL 模式下 NORMAL 是平衡点
PRAGMA temp_store = MEMORY;
PRAGMA busy_timeout = 10000;                            -- 10s 写锁等待
```

---

## 4. SQL 后端抽象层（DAO Protocol + 双实现）

不上 ORM——写薄 DAO（一组 repository）。每个 method 是具体业务查询；后端切换 = 换 repository 实现。

### 4.1 设计原则

- **Protocol 只暴露业务原子操作**，不暴露 SQL 字符串
- **事务边界在 service 层**，repository 只做原子单步
- 不用 ORM 魔法（subquery 自动生成、复杂 join 等）——这些在两边方言差异最大
- 每个 method 在 SQLite + Postgres 各写一次，但 method 总数估计 30-50 个

### 4.2 Protocol 草案（伪代码）

```python
class TaskRepository(Protocol):
    def create(spec: TaskSpec) -> str: ...
    def get(task_id: str) -> Task | None: ...
    def claim_lease(task_id: str, holder_wakeup_id: str, duration_sec: int) -> bool: ...
    def renew_lease(task_id: str, holder_wakeup_id: str, duration_sec: int) -> bool: ...
    def release_lease(task_id: str, holder_wakeup_id: str) -> None: ...
    def update_checkpoint(task_id: str, checkpoint: dict, holder_wakeup_id: str) -> None: ...
    def find_expired_leases(limit: int) -> list[Task]: ...
    def update_status(task_id: str, status: str) -> None: ...
    def find_children(parent_task_id: str) -> list[Task]: ...      # child task 列表（query / poll）

class WakeupRepository(Protocol):
    def start(task_id: str, persona_name: str) -> str: ...        # 返回 wakeup_id
    def end(wakeup_id: str, status: str, metering: dict, failure: dict | None) -> None: ...
    def set_transcript_uri(wakeup_id: str, uri: str) -> None: ...

class MailboxRepository(Protocol):
    # 0005 切到 per-message read state——`mailbox_read` 默认拉 unread + 立即
    # 写 read_at；显式 `mark_messages_read` 给 mark_read 工具用。
    def read_unread(recipient: str, *, min_urgency: str | None = None,
                    limit: int = 50) -> list[Message]: ...
    def read_all_by_recipient(recipient: str, *, limit: int = 50) -> list[Message]: ...
    def mark_messages_read(recipient: str, msg_ids: list[int]) -> None: ...
    def count_unread(recipient: str, *, min_urgency: str | None = None) -> int: ...
    def list_sent_by(sender: str, *, recipient: str | None = None,
                     limit: int = 50) -> list[Message]: ...
    def get_message(msg_id: int) -> Message | None: ...     # 跨 mailbox 查单条

class OutboxRepository(Protocol):
    def enqueue(rows: list[OutboxRow]) -> None: ...               # 在 commit point 调
    def dequeue_batch(limit: int) -> list[OutboxRow]: ...         # 单 dispatcher claim
    def mark_dispatched(row_id: int) -> None: ...
    def mark_failed(row_id: int, error: str, permanent: bool) -> None: ...

class PersonaRepository(Protocol):
    def get(name: str) -> Persona | None: ...
    def list_active(status: str = 'approved') -> list[Persona]: ...
    def propose(name: str, role_description: str, system_prompt: str,
                allowed_lyre_tools: list[str], source_task_id: str, ...) -> str: ...
    def approve(persona_name: str, reviewer: str,
                status: Literal['approved','deprecated'], comment: str | None) -> None: ...
    # No profile/upsert methods — owner identity lives in ~/.lyre/user.md (file-only).

class LocalHotRepository(Protocol):
    def put(task_id: str, key: str, value: Any) -> None: ...
    def get(task_id: str, key: str) -> Any | None: ...
    def clear_task(task_id: str) -> None: ...                     # 任务完成时调

# No GlobalFactsRepository — facts are markdown files under ~/.lyre/memory/facts/
# managed by agents via grep / shell_exec / read_memory. No DB rows, no embeddings.

class ArtifactRepository(Protocol):
    def insert(task_id: str, wakeup_id: str, kind: str, content_hash: str,
               blob_uri: str, size: int) -> str: ...
    def get_by_hash(content_hash: str) -> Artifact | None: ...    # 去重检查

class SkillRepository(Protocol):
    def propose(name: str, frontmatter: dict, body: str,
                source_task_id: str) -> str: ...                  # status=proposed
    def approve(skill_id: str, reviewer: str) -> None: ...        # status=approved
    def deprecate(skill_id: str) -> None: ...                     # status=deprecated
    def get_by_name(name: str) -> Skill | None: ...               # load_skill 用
    def search_for_context(persona_name: str, scope: str | None,
                           limit: int) -> list[SkillFrontmatter]: ...   # progressive disclosure：只返 frontmatter

class CommitContext(Protocol):                                    # 跨 repository 单事务
    def __enter__(self) -> Self: ...
    def __exit__(self, *args) -> None: ...
    # 业务代码：
    # with ctx.transaction() as t:
    #     t.tasks.update_checkpoint(...)
    #     t.outbox.enqueue([...])
    #     t.artifacts.insert(...)
    # commit 是 with 块退出时自动；异常则 rollback
```

### 4.3 两个实现：`SqliteRepositories` / `PostgresRepositories`

```python
class SqliteRepositories:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path, isolation_level=None)  # 手动事务
        self.conn.execute("PRAGMA journal_mode=WAL")
        # ... 其它 PRAGMA
        self.tasks = SqliteTaskRepository(self.conn)
        self.wakeups = SqliteWakeupRepository(self.conn)
        # ...

class PostgresRepositories:
    def __init__(self, dsn: str):
        self.pool = psycopg.AsyncConnectionPool(dsn)
        self.tasks = PostgresTaskRepository(self.pool)
        # ...

# 业务代码注入哪个：
repos: Repositories = SqliteRepositories('./lyre.db')   # 或 PostgresRepositories('postgres://...')
```

---

## 5. 并发与事务策略

### 5.1 SQLite 写锁特性

- SQLite 写锁是 **db 级**（一次只一个 writer）
- WAL 模式下读不阻塞写、写不阻塞读
- 写争用时新写者等到 `busy_timeout`（设 10s）后报错
- MVP QPS 低，写锁等待几乎不发生

### 5.2 关键操作的并发模式

**Task lease 抢占（单语句原子）**：

```sql
UPDATE tasks
SET lease_holder = :wakeup_id,
    lease_until = :new_until
WHERE id = :task_id
  AND (lease_until IS NULL OR lease_until < :now)
RETURNING id;
```

`RETURNING id` 影响 0 行 → 别人抢先；新 agent 退让。

**Outbox dequeue（单 dispatcher 进程，单事务）**：

```sql
BEGIN;
SELECT * FROM outbox
WHERE dispatched_at IS NULL
ORDER BY created_at
LIMIT 100;
-- dispatcher in-memory 处理一批
UPDATE outbox SET dispatched_at = :now WHERE id IN (:ids);
COMMIT;
```

MVP 单 dispatcher 进程不需要 `SKIP LOCKED`。

**Commit point（[TRANSACTION_BOUNDARIES.md §2 Step 9](./TRANSACTION_BOUNDARIES.md)）**：

```python
with repos.transaction() as t:
    t.tasks.update_checkpoint(task_id, new_checkpoint, wakeup_id)
    t.artifacts.insert(...)                  # 多次
    t.outbox.enqueue([...])                  # 派生 mailbox 消息 / Tier 1 通知
    # Mailbox read state 不在这里推进——`mailbox_read` 工具调用时已经写了
    # 各 msg 的 read_at（0005 之后是 per-message 而非 cursor），所以
    # commit point 不再需要专门"推进 mailbox 偏移"那一步。
    t.tasks.release_lease(task_id, wakeup_id)
# with 退出时 commit；异常 rollback
```

SQLite 单 writer + 单事务原子，跨表写入一致性满足 [FOUNDATION §4 第一条](./FOUNDATION.md#4-工程后果拔线测试的三条硬约束)。

### 5.3 跨进程并发

- **Lyre 主进程** 写 tasks（派任务、状态更新）+ 派 agent subprocess
- **多 agent subprocess** 通过 Lyre gateway 间接写 mailbox / progress（gateway 进程是 writer，主进程也是 writer——共用同一个 SQLite 连接池更稳）
- **Dispatcher 进程** 写 outbox / mailbox_messages

**MVP 架构建议**：所有持久层访问都过 **Lyre 主进程内的单一 connection pool**——dispatcher 与 gateway 都是主进程内的子任务（asyncio coroutine 或 thread），不开新进程。如此 SQLite 写锁竞争最小化。

需要多进程时再切 Postgres——届时 dispatcher 与 gateway 可独立进程。

---

## 6. Object store 路径约定

| 用途 | 路径模板 |
|---|---|
| Artifact blob（去重） | `artifacts/{content_hash}` |
| Wakeup transcript | `wakeups/{wakeup_id}/transcript.jsonl` |
| Wakeup metering 大日志 | `wakeups/{wakeup_id}/metering.jsonl` |
| Wakeup tool call 日志 | `wakeups/{wakeup_id}/tool_calls.jsonl` |
| Local-hot 大条目 | `local_hot/{task_id}/{key}` |

**MVP 实现**：本地文件系统下 `./object_store/` 目录，对应路径直接 mkdir + 写文件。`blob_uri` 字段格式 `file://./object_store/{path}`。

**切 S3 / MinIO**：换 `ObjectStore` 适配层，`blob_uri` 改 `s3://bucket/{path}`。业务代码不变。

**阈值**（v0.1 暂定）：

- Artifact >1 MB → object store；其它直接 inline 在 `artifacts.metadata`（少量 base64 也可接受）
- Local-hot value >256 KB → object store + 设 `blob_uri`；其它直接 JSON

---

## 7. Migration 路径（SQLite → Postgres）

切的信号见 §1。切的步骤：

1. **新 Postgres 实例**：起好库，跑 PG 等价 DDL（schema 结构 95% 相同，仅类型差异）
2. **dump SQLite → restore PG**：用 `sqlite3 .dump | (PG-compatible cleanup) | psql`，或写个一次性脚本逐表 COPY
3. ~~嵌入向量重建~~ — 已删除（不再有 sqlite-vec / pgvector，没有 embedding 列）。
4. **切配置**：`SqliteRepositories(...)` → `PostgresRepositories(...)`
5. **冒烟测试**：跑一次端到端任务 + 拔线测试
6. **切流量**：因为是 owner 自用、单实例，切流量就是重启 Lyre

不需要 zero-downtime migration（owner 重启可忍）。

---

## 8. 已识别但待解决的问题

> 起草过程中浮现的子问题。

1. **`mailboxes.recipient` 命名约定**：是 `'owner'` / `'persona:leader'` / `'persona:worker-maintainer'` 这样带前缀，还是统一加 type 字段？前缀方案对调试友好但容易拼错。v0.1 倾向带前缀字符串 + lint 工具校验
2. ~~Embedding 模型与维度~~ — 不适用，向量层已删。
3. **Persona PK 选 name 还是 UUID**：v0.1 用 name 作 PK（人类友好，例如 `'leader'`），foreign key 都引 name。改名变成 schema migration 痛点，但 MVP 阶段 persona 集合稳定，可接受
4. **Local-hot 删除时 cold-archive snapshot**：任务完成时 `ON DELETE CASCADE` 清 `local_hot`，但有些 local_hot 内容（如 agent 尝试过的方案）对 cold-archive 研究有价值。v0.1 不做 snapshot；如果需要，前缀 `archive:` 的 key 在删前 dump 到 object store
5. **Outbox 分区**：MVP 不分区；高量级时按 `created_at` 月份分区（PG 原生支持，SQLite 不支持 → 切 PG 时再做）
6. **`needs_worktree` 字段**：v0.1 在 `personas` 表标 boolean；但实际"需要 worktree 与否"可能 task-specific 而非 persona-specific。考虑挪到 `tasks` 表 / TaskSpec
7. **多 Lyre 实例 / 多 owner 支持**：MVP 单实例单 owner；未来加 region / owner_id 字段
8. **SQLite WAL 跨进程访问**：WAL 模式跨进程读写需要 fsync 友好的文件系统（macOS APFS / Linux ext4 都 OK，但某些网络文件系统不行）。Owner 把 `.db` 放在普通本地磁盘即可
9. **Cold archive 的"自动归档"触发**：task `status=completed` 后多久把 transcript 从 wakeups 表外推到 object store？v0.1 倾向"任务完成即归档"，但 cold archive 路径 / 索引由谁建？需要补一个 archive job
10. **Skill 检索算法**：`SkillRepository.search_for_context` 的 ranking 算法——纯向量？关键词匹配 + 向量混合？persona 兼容性怎么编码？v0.2 留待 prototype 验证
11. **Skill 审核 workflow 的持久化**：proposed → approved 转移要不要走 mailbox（reviewer-persona 收到 mailbox 消息）？还是 reviewer 主动定时扫 `status=proposed`？v0.2 倾向 mailbox-driven
12. **`owner` 这一行的 persona spec 形态**：owner 不是 LLM agent，但需要占 `personas` 表一行（为了 `agents` 表 `id='owner'` 的 FK）。系统对 owner 的 `system_prompt` 字段如何处理（空字符串？）；`needs_worktree=0`。当前留空字符串，应用层避开。

---

