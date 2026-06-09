# Lyre — memory organization(facts 的整理与淘汰)

> **文档定位**:`~/.lyre/memory/facts/` 长期是**纯平铺、无整理、无淘汰**。本轮不推翻
> FOUNDATION §3.8 的赌注(facts = agent 用 grep/shell 自管理 markdown,**不上**向量库 /
> promotion 流水线 / eviction 机器),而是补上三件**便宜、守 §3.8、过得了 observed-not-
> theoretical 尺**的事,并**明确记录**哪些更重的改动被 defer 及其复活条件。
>
> **English one-liner**: Make `facts/` organizable + curatable WITHOUT re-opening FOUNDATION
> §3.8's "no vector DB / no promotion / no eviction machinery" bet. Ship three cheap,
> §3.8-faithful changes (index groups by an already-written `type` field; a zero-code
> `facts/archive/` convention; a one-line persona nudge that gives the agent the curation
> trigger the harness lacks) and explicitly record the four heavier items as deferred, with
> revival triggers.
>
> **来历**:一次对抗式辩论得出"全 defer",owner 追问"defer 的话 type-grouping 何时启动?";
> 复盘发现原辩论的 prompt **预先焊入了 owner 的 bar**(+ 一条只猎 over-build 的 critic 腿、
> 无 cost-of-waiting 一方),倾向 defer 是**部分 framing 产物**。一次**再平衡**辩论
> (build-now advocate + framing auditor + steelman-defer,三腿)在**去掉 bar** 后收敛:
> 三件 NOW、四件 robust-DEFER——连为 defer 辩护的那腿都让步认了前两件 NOW。

---

## 1. 一个贯穿全局的结构事实

`scan_memory_dir`([memory.py](../../src/lyre/runtime/memory.py))**非递归**(`d.iterdir()` +
`is_file()`,跳过子目录)。这把同一个机制**两面**都是 load-bearing:

- **是 bug**:为"组织"而把 fact 嵌进 `facts/sub/x.md` → 它**静默从 `## Available global
  memory` 菜单消失**(实测复现:文件真实可 `read_memory`,但不进菜单)。
- **是 feature**:`facts/archive/` 里的 fact **天然不进菜单**,却仍 `grep` / `read_memory`
  得到。

→ **绝不让扫描递归**。递归会同时(a)毁掉这个免费的 archive 不可见性,(b)把过时/归档的
fact 重新塞回每个 wakeup 的 prompt。"用子目录组织"(Scheme D)因此不是 defer,而是**错的
方向**。正确形状是:**`type` frontmatter 分组(展示层)+ `archive/` 子目录(淘汰层)**。

## 2. 本轮做的三件(NOW)

### (1) 索引按 `type` 分组(唯一的代码改动)
`type:` frontmatter 字段**早就被每个 fact 作者写着**(analyst 的 `type: spec`
[analyst.md:56],出厂 checklist 的 `type: review_checklist`,notes 的 `type: agent_notes`),
但渲染器**一直丢掉它**——`_format_line` 只读 description+scope,分组键 `e.kind` 是 layout
里硬编码的字面量 `"fact"`,不是 frontmatter。`_format_fact_lines`([memory.py](../../src/lyre/runtime/memory.py))
现在按 `MemoryEntry.type` 分组渲染。**自适应**:只有一种 type(或全无 type)时退化成**和
以前一模一样的平铺**——低量零成本,菜单一多才显出分组。**无新字段、无新写路径、无生命
周期**,纯展示。§3.8 本身就背书"索引只用文件名 + frontmatter"(FOUNDATION.md:259)——分组
一个已存在的 frontmatter 字段**就在**被许可的设计空间内。

### (2) `facts/archive/` 约定(零代码)
靠 §1 的非递归扫描天然成立:agent 把**过时 / 被取代**的 fact `mv` 进
`~/.lyre/memory/facts/archive/` → 移出菜单、仍可 `grep`/`read_memory`、可逆、零丢失。这是
"**语义归档,不是机械按 age 删**"(facts ≠ 时序 log,年龄 ≠ 过时)。`ensure_skeleton` 预建
该目录让 `mv` 即用。仿已有的 `notes_archive/` 先例。

### (3) 一行 persona 提示——facts 的整理触发器(最关键的发现)
facts 是这套记忆里**唯一没有任何整理脚手架**的桶:scratchpad 有 32KiB 硬顶 + prune 训练 +
preamble 反复叮嘱;notes 有 auto-summary + 轮转;**facts 什么都没有**。所以"agent 自管理
facts"的赌注在实践中**等于从不发生**——不是因为 agent 不能(analyst 有 shell/python),而是
**从没有任何东西提示它去做**。[analyst.md](../../src/lyre/personas/analyst.md) 的【Memory 写
权限】加一行:开工写新 spec 前扫 `facts/`、把过时的 `mv` 进 `archive/`。这是**最 §3.8-忠实**的
一步——§3.8 赌"agent 用 grep/shell 自管理",而这行就是那条**一直缺席的指令**。它也是 (2) 的
**触发器**:没有它,archive 约定"准许但没人用"。

> **只加在 analyst**:它是唯一的常规**共享** facts 写者。worker 写 `facts/` 是 Tier-2 受控
> (worker-maintainer.md:90),reviewer 被明确告知"其它子目录不要碰"(reviewer.md:94)。

## 3. 明确 DEFER 的四件(robust,去掉 owner-bar 也站得住)

| 项 | 为何 defer(非 framing 产物) | 复活触发 |
|---|---|---|
| `status: active\|archived` flag | 没人写的新字段 + 让 index 理解一套生命周期 = §3.8 拒绝的 promotion 压力;`archive/`-by-move 已覆盖同一需求、零代码 | 观测到"fact 必须留在列表但标 deprecated、不能移走"的真实场景 |
| 递归子目录扫描 | **净负**:会毁掉 §1 的免费 archive 不可见性,把 `archive/` 重新塞回菜单,反而要再加 ignore-list 找补 | 几乎无;真要深层组织且 archive 另有他法时再议 |
| `write_memory` 工具 | 新工具面 + 路径选择策略 + 第二条写路径要保持一致,纯 harness 复杂度;`python_exec`/`shell_exec` 写已够用 | 观测到非 code-tool persona 也需要写 facts、且 shell 写不安全/不够 |
| `staleness` 字段 | **最硬的 no**:没有任何刷新信号——一个写一次永不更新的 staleness = 比没有更糟的误导("缺 stale 标"会被读成"新鲜",实为"没核对过")。该字段预设一个 consolidation 触发器 = §3.8 拒绝的机器 | 先有 deliberate 设计的 consolidation/observer pass,再谈字段 |

**共识**:这四件**在去掉 bar 的中立 prompt 下依然 defer**——它们各自引入真实的低量成本,或
重新引入 §3.8 刻意拒绝的生命周期机器。owner 的 bar 在这四件上**做对了工**;它只在前两件
(零代码展示项)上因 framing 误判成了 defer。

## 4. 仍未解决的根问题(记录,不本轮做)

复盘暴露的真正根因:**facts 没有任何 staleness 信号 / consolidation occasion**(Q3)。本轮的
(3) 用"agent 自检"这个**零成本触发器**部分缓解,但系统级的"何时该整理"仍**无观测者**。若
未来 `ls ~/.lyre/memory/facts/` 真出现上百条跨多领域的 fact,届时再考虑:把 (1) 的分组做深、
或加一个**周期性 self-mail 的 memory-consolidation 反射**(coordination-imagination skill 里
那颗"sleep cycle"种子)——但那是观测驱动的下一步,不是现在。

## 5. 五铁律 / kill-test

- **铁律一**:纯 FS + 渲染逻辑,不碰 `adapter/`。
- **铁律三(拔线)**:无新持久态;archive 是 `mv`(原子 rename),分组是纯读渲染,kill 无新失败面。
- **铁律四**:不上向量库 / promotion / eviction 机器——守 §3.8。archive 是冷化的反面(facts 仍是
  热的全局层,只是移出菜单),不回读进 runtime 的语义未变。
- **铁律五**:不涉通信。
